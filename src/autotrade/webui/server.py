"""FastAPI application for the HITL experiment console.

JSON API + static SPA. The server is a thin control plane: pipeline execution
happens in detached worker processes; state flows through the hitl/ files and
the append-only ledger. Binds 127.0.0.1 by default; there is no auth layer, so
non-local binds should only be used behind a trusted reverse proxy.
"""

from __future__ import annotations

import tempfile
import threading
import zipfile
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from autotrade.pipelines.fold_analysis import analysis_paths, analyze_fold
from autotrade.pipelines.interactive import ANALYSIS_DIR_NAME, HITL_DIR_NAME, PARAMS_NAME, STATUS_NAME, read_json, read_status

from . import registry
from .manager import ExperimentManager, ManagerError, MAX_RUNNING_EXPERIMENTS
from .params_schema import parameter_schema
from .traces import read_trace_page, resolve_trace_path, stream_trace, trace_stats

# Datasets whose partition coverage bounds the selectable backtest periods: the
# replay needs minute bars, so their intersection with the daily lake is the
# honest "data exists" window (issue: pre-coverage periods fail at runtime).
_COVERAGE_DATASETS = ("daily", "stk_mins_1min_by_date")


def _dataset_coverage(raw_dir: Path, dataset: str) -> tuple[str, str] | None:
    root = raw_dir / dataset
    if not root.is_dir():
        return None
    dates = [
        entry.name[len("trade_date="):-len(".parquet")]
        for entry in root.glob("trade_date=*.parquet")
    ]
    dates = [d for d in dates if len(d) == 8 and d.isdigit()]
    if not dates:
        return None
    return min(dates), max(dates)

STATIC_DIR = Path(__file__).resolve().parent / "static"


class AnalysisService:
    """Background (re)generation of fold analyses, one at a time per fold."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)
        self._pending: set[tuple[str, str, str]] = set()
        self._lock = threading.Lock()

    def pending(self, experiment_id: str, epoch_id: str, fold_id: str) -> bool:
        with self._lock:
            return (experiment_id, epoch_id, fold_id) in self._pending

    def regenerate(self, experiments_root: Path, experiment_id: str, epoch_id: str, fold_id: str) -> None:
        key = (experiment_id, epoch_id, fold_id)
        with self._lock:
            if key in self._pending:
                raise ManagerError("analysis for this fold is already being generated")
            self._pending.add(key)
        try:
            experiment_dir = registry.resolve_experiment_dir(experiments_root, experiment_id)
            detail = registry.fold_detail(experiments_root, experiment_id, epoch_id, fold_id)
            strategy_dir = detail.get("strategy_dir")
            if not strategy_dir or not Path(str(strategy_dir)).is_dir():
                raise ManagerError("fold has no frozen strategy artifact on disk")
            params = read_json(experiment_dir / HITL_DIR_NAME / PARAMS_NAME)
            model = str(params.get("analysis_model") or "deepseek-v4-pro")
            max_tokens = int(params.get("analysis_max_tokens") or 6000)
            record = dict(detail["record"])
            model_dir = record.get("frozen_model_artifact_path")
        except Exception:
            with self._lock:
                self._pending.discard(key)
            raise

        def _run() -> None:
            try:
                from autotrade.environment.llm import DeepSeekProxy

                proxy = DeepSeekProxy.from_env(
                    model=model,
                    env_file=str(self.repo_root / ".env"),
                    thinking_enabled=True,
                    reasoning_effort="high",
                )
                analyze_fold(
                    proxy,
                    ledger_record=record,
                    strategy_dir=Path(str(strategy_dir)),
                    model_dir=Path(str(model_dir)) if model_dir else None,
                    out_dir=experiment_dir / HITL_DIR_NAME / ANALYSIS_DIR_NAME,
                    max_tokens=max_tokens,
                )
            except Exception:  # noqa: BLE001 - failure lands in the sidecar json
                pass
            finally:
                with self._lock:
                    self._pending.discard(key)

        threading.Thread(target=_run, name=f"analysis-{experiment_id}-{fold_id}", daemon=True).start()


def create_app(repo_root: Path, experiments_root: Path | None = None) -> FastAPI:
    repo_root = Path(repo_root).resolve()
    manager = ExperimentManager(repo_root, experiments_root)
    analysis_service = AnalysisService(repo_root)
    app = FastAPI(title="CornerHead Console", docs_url=None, redoc_url=None)
    trading_days_cache: dict[str, list[str] | None] = {}

    def _trading_days() -> list[str]:
        # Loaded once per process; without a calendar (dev/test roots) the
        # period pickers degrade to text inputs instead of failing the schema.
        # The calendar is clamped to the datasets' actual partition coverage so
        # the pickers cannot offer periods without downloaded/processed data.
        if "days" not in trading_days_cache:
            try:
                from autotrade.pipelines.folds import load_sse_trading_days

                raw_dir = repo_root / "data" / "raw"
                days = load_sse_trading_days(raw_dir)
                coverages = [c for c in (_dataset_coverage(raw_dir, name) for name in _COVERAGE_DATASETS) if c]
                if coverages:
                    low = max(c[0] for c in coverages)
                    high = min(c[1] for c in coverages)
                    days = [day for day in days if low <= day <= high]
                trading_days_cache["days"] = days
            except Exception:  # noqa: BLE001 - schema must stay served
                trading_days_cache["days"] = None
        return trading_days_cache["days"] or []

    def _experiment_dir(experiment_id: str) -> Path:
        try:
            return registry.resolve_experiment_dir(manager.experiments_root, experiment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ---- meta ----------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "status": "ok",
            "experiments_root": str(manager.experiments_root),
            "max_running_experiments": MAX_RUNNING_EXPERIMENTS,
            "running": manager.running_experiments(),
        }

    @app.get("/api/parameter-schema")
    def get_parameter_schema() -> dict[str, object]:
        return parameter_schema(trading_days=_trading_days())

    # ---- experiments -----------------------------------------------------------
    @app.get("/api/experiments")
    def get_experiments() -> dict[str, object]:
        return {
            "experiments": registry.list_experiments(manager.experiments_root),
            "running": manager.running_experiments(),
            "max_running_experiments": MAX_RUNNING_EXPERIMENTS,
        }

    @app.post("/api/experiments")
    def post_experiment(payload: dict = Body(...)) -> dict[str, object]:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else payload
        try:
            return manager.create_experiment(dict(params))
        except (ManagerError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/experiments/{experiment_id}")
    def get_experiment(experiment_id: str) -> dict[str, object]:
        _experiment_dir(experiment_id)
        return registry.experiment_detail(manager.experiments_root, experiment_id)

    @app.delete("/api/experiments/{experiment_id}")
    def delete_experiment(experiment_id: str, confirm: str = Query("")) -> dict[str, object]:
        _experiment_dir(experiment_id)
        if confirm != experiment_id:
            raise HTTPException(status_code=400, detail="confirm query param must equal the experiment id")
        try:
            return manager.delete_experiment(experiment_id)
        except ManagerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/experiments/{experiment_id}/control")
    def post_control(experiment_id: str, payload: dict = Body(...)) -> dict[str, object]:
        _experiment_dir(experiment_id)
        try:
            return manager.control(
                experiment_id,
                str(payload.get("action") or ""),
                session_key=payload.get("session_key"),
                directive=payload.get("directive"),
                mode=payload.get("mode"),
            )
        except ManagerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/experiments/{experiment_id}/status")
    def get_status(experiment_id: str) -> dict[str, object]:
        experiment_dir = _experiment_dir(experiment_id)
        return {
            **registry.experiment_state(experiment_dir),
            "raw_status": read_status(experiment_dir / HITL_DIR_NAME / STATUS_NAME),
        }

    # ---- folds -------------------------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/folds/{epoch_id}/{fold_id}")
    def get_fold(experiment_id: str, epoch_id: str, fold_id: str) -> dict[str, object]:
        _experiment_dir(experiment_id)
        try:
            detail = registry.fold_detail(manager.experiments_root, experiment_id, epoch_id, fold_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        detail["analysis"]["pending"] = analysis_service.pending(experiment_id, epoch_id, fold_id)
        return detail

    @app.get("/api/experiments/{experiment_id}/folds/{epoch_id}/{fold_id}/strategy-file")
    def get_strategy_file(experiment_id: str, epoch_id: str, fold_id: str, path: str = Query(...)) -> PlainTextResponse:
        _experiment_dir(experiment_id)
        try:
            detail = registry.fold_detail(manager.experiments_root, experiment_id, epoch_id, fold_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        strategy_dir = detail.get("strategy_dir")
        if not strategy_dir:
            raise HTTPException(status_code=404, detail="fold has no frozen strategy artifact")
        root = Path(str(strategy_dir)).resolve()
        target = (root / path).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            raise HTTPException(status_code=404, detail="file not found in strategy artifact")
        if target.stat().st_size > 2_000_000:
            raise HTTPException(status_code=413, detail="file too large to inline; download the zip instead")
        return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))

    @app.get("/api/experiments/{experiment_id}/folds/{epoch_id}/{fold_id}/strategy.zip")
    def get_strategy_zip(experiment_id: str, epoch_id: str, fold_id: str) -> FileResponse:
        _experiment_dir(experiment_id)
        try:
            detail = registry.fold_detail(manager.experiments_root, experiment_id, epoch_id, fold_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        strategy_dir = detail.get("strategy_dir")
        if not strategy_dir or not Path(str(strategy_dir)).is_dir():
            raise HTTPException(status_code=404, detail="fold has no frozen strategy artifact on disk")
        record = detail["record"]
        model_dir = record.get("frozen_model_artifact_path")
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        zip_path = Path(handle.name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            root = Path(str(strategy_dir))
            for file in sorted(root.rglob("*")):
                if file.is_file():
                    archive.write(file, Path("output") / file.relative_to(root))
            if model_dir and Path(str(model_dir)).is_dir():
                model_root = Path(str(model_dir))
                for file in sorted(model_root.rglob("*")):
                    if file.is_file():
                        archive.write(file, Path("models") / file.relative_to(model_root))
        filename = f"{experiment_id}__{epoch_id}__{fold_id}.zip"
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(zip_path.unlink, missing_ok=True),
        )

    # ---- fold orders ----------------------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/folds/{epoch_id}/{fold_id}/orders")
    def get_fold_orders(
        experiment_id: str, epoch_id: str, fold_id: str, result: str | None = Query(None)
    ) -> dict[str, object]:
        _experiment_dir(experiment_id)
        try:
            return registry.fold_orders(manager.experiments_root, experiment_id, epoch_id, fold_id, result=result)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/experiments/{experiment_id}/folds/{epoch_id}/{fold_id}/orders.csv")
    def get_fold_orders_csv(
        experiment_id: str, epoch_id: str, fold_id: str, result: str = Query(...)
    ) -> PlainTextResponse:
        _experiment_dir(experiment_id)
        try:
            filename, csv_text = registry.fold_orders_csv(
                manager.experiments_root, experiment_id, epoch_id, fold_id, result=result
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return PlainTextResponse(
            csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    # ---- analysis -----------------------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/analysis/{epoch_id}/{fold_id}")
    def get_analysis(experiment_id: str, epoch_id: str, fold_id: str) -> dict[str, object]:
        experiment_dir = _experiment_dir(experiment_id)
        md_path, meta_path = analysis_paths(experiment_dir / HITL_DIR_NAME / ANALYSIS_DIR_NAME, epoch_id, fold_id)
        return {
            "available": md_path.exists(),
            "pending": analysis_service.pending(experiment_id, epoch_id, fold_id),
            "content": md_path.read_text(encoding="utf-8") if md_path.exists() else None,
            "meta": read_json(meta_path) if meta_path.exists() else None,
        }

    @app.post("/api/experiments/{experiment_id}/analysis/{epoch_id}/{fold_id}")
    def post_analysis(experiment_id: str, epoch_id: str, fold_id: str) -> dict[str, object]:
        _experiment_dir(experiment_id)
        try:
            analysis_service.regenerate(manager.experiments_root, experiment_id, epoch_id, fold_id)
        except (ManagerError, KeyError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "started"}

    # ---- prompt preview -------------------------------------------------------------
    @app.post("/api/experiments/{experiment_id}/prompt-preview")
    def post_prompt_preview(experiment_id: str, payload: dict = Body(...)) -> dict[str, object]:
        """Assemble the session's system prompt for pre-approval review.

        The runtime-generated 当前实验事实 JSON block (built from the live run
        manifest/runtime_env/data_summary) cannot exist before the sandbox is
        prepared, so the preview renders the documented fallback (fold info +
        acceptance rules verbatim); every other section — role, environment,
        taste, researcher directive, actions, contract, prohibitions — is the
        exact text the Agent will receive.
        """
        experiment_dir = _experiment_dir(experiment_id)
        session_key = str(payload.get("session_key") or "")
        directive = str(payload.get("directive") or "")
        hitl_dir = experiment_dir / HITL_DIR_NAME
        schedule = read_json(hitl_dir / "schedule.json")
        sessions = schedule.get("sessions") if isinstance(schedule.get("sessions"), list) else []
        entry = next((s for s in sessions if s.get("key") == session_key), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"unknown session: {session_key}")
        kind = str(entry.get("kind"))
        if kind == "heldout":
            raise HTTPException(status_code=400, detail="held-out runs have no agent session or system prompt")
        params = read_json(hitl_dir / PARAMS_NAME)
        from autotrade.pipelines.interactive import PARAM_DEFAULTS
        from autotrade.agent.prompts import build_meta_learning_prompt, build_system_prompt

        def param(key: str):
            return params.get(key, PARAM_DEFAULTS.get(key))

        if kind == "meta_learning":
            prompt = build_meta_learning_prompt(
                experiment_directive=directive.strip() or str(param("meta_learning_directive") or ""),
            )
        else:
            epoch_id = str(entry.get("epoch_id") or "epoch_001")
            try:
                epoch_index = int(epoch_id.rsplit("_", 1)[-1])
            except ValueError:
                epoch_index = 1
            taste = ""
            for record in registry._read_ledger_records(experiment_dir):
                if record.get("record_type") == "meta_learning" and str(record.get("epoch_id")) == epoch_id:
                    taste_path = record.get("taste_path")
                    if taste_path and Path(str(taste_path)).exists():
                        taste = Path(str(taste_path)).read_text(encoding="utf-8").strip()
            # The runtime facts block redacts the test schedule from the agent;
            # keep the preview's fallback consistent (no test_period).
            fold_info = {
                key: entry.get(key)
                for key in ("fold_id", "input_window", "validation_period", "valid_decision_time")
                if entry.get(key) is not None
            }
            prompt = build_system_prompt(
                fold_info=fold_info,
                acceptance_rules={
                    "min_return": param("min_return"),
                    "min_sharpe": param("min_sharpe"),
                    "max_drawdown": param("max_drawdown"),
                    "require_complete_validation": True,
                },
                phase="convergence" if epoch_index >= int(param("convergence_start_epoch") or 3) else "exploration",
                step_tree_enabled=not bool(param("disable_step_tree")),
                taste_prompt=taste,
                fold_directive=directive,
            )
        return {
            "kind": kind,
            "prompt": prompt,
            "note": (
                "预览包含 Agent 将收到的全部静态段（角色/环境/Taste/研究者指令/动作/提交合同/禁止行为）。"
                "运行时「当前实验事实」JSON 由沙箱准备完成后的 run manifest 等生成，此处以 Fold 信息与验收规则原文代替；"
                "该块只是事实索引，不含额外指令。"
            ),
        }

    # ---- traces --------------------------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/trace/stats")
    def get_trace_stats(experiment_id: str, run_id: str | None = Query(None)) -> dict[str, object]:
        experiment_dir = _experiment_dir(experiment_id)
        path = resolve_trace_path(experiment_dir, run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no trace available for this run")
        return {"trace_path": str(path), **trace_stats(path)}

    @app.get("/api/experiments/{experiment_id}/trace/download")
    def get_trace_download(experiment_id: str, run_id: str | None = Query(None)) -> FileResponse:
        experiment_dir = _experiment_dir(experiment_id)
        path = resolve_trace_path(experiment_dir, run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no trace available for this run")
        filename = f"{experiment_id}__{run_id or path.parent.name}__agent_trace.jsonl"
        return FileResponse(path, media_type="application/x-ndjson", filename=filename)

    @app.get("/api/experiments/{experiment_id}/trace")
    def get_trace(
        experiment_id: str,
        run_id: str | None = Query(None),
        offset: int = Query(0, ge=0),
    ) -> dict[str, object]:
        experiment_dir = _experiment_dir(experiment_id)
        path = resolve_trace_path(experiment_dir, run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no trace available for this run")
        return {"trace_path": str(path), **read_trace_page(path, offset=offset)}

    @app.get("/api/experiments/{experiment_id}/trace/stream")
    def get_trace_stream(
        experiment_id: str,
        run_id: str | None = Query(None),
        offset: int = Query(0, ge=0),
    ) -> StreamingResponse:
        experiment_dir = _experiment_dir(experiment_id)
        return StreamingResponse(
            stream_trace(experiment_dir, run_id, offset=offset),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ---- static SPA -----------------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        @app.get("/", response_class=HTMLResponse)
        def index() -> HTMLResponse:
            return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))

    return app


def run(
    repo_root: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 38888,
    uds: Path | None = None,
    experiments_root: Path | None = None,
) -> None:
    import signal

    import uvicorn

    # Auto-reap detached workers so exited experiments never linger as zombies
    # (their liveness is judged via status.json pid checks).
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    app = create_app(repo_root, experiments_root)
    if uds is not None:
        # Unix-socket bind: local access control is the parent directory's
        # filesystem permissions (loopback TCP is reachable by every local
        # user on a shared host). uvicorn chmods the socket itself to 666,
        # so the caller must keep the directory 0700.
        uvicorn.run(app, uds=str(uds), log_level="info")
    else:
        uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["create_app", "run"]

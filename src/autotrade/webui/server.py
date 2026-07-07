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
from .traces import read_trace_page, resolve_trace_path, stream_trace

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
        if "days" not in trading_days_cache:
            try:
                from autotrade.pipelines.folds import load_sse_trading_days

                trading_days_cache["days"] = load_sse_trading_days(repo_root / "data" / "raw")
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

    # ---- traces --------------------------------------------------------------------
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


def run(repo_root: Path, *, host: str = "127.0.0.1", port: int = 38888, experiments_root: Path | None = None) -> None:
    import signal

    import uvicorn

    # Auto-reap detached workers so exited experiments never linger as zombies
    # (their liveness is judged via status.json pid checks).
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    app = create_app(repo_root, experiments_root)
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["create_app", "run"]

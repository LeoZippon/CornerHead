"""FastAPI application for the HITL experiment console.

JSON API + static SPA. The server is a thin control plane: pipeline execution
happens in detached worker processes; state flows through the hitl/ files and
the append-only ledger. Binds 127.0.0.1 by default; there is no auth layer, so
non-local binds should only be used behind a trusted reverse proxy.
"""

from __future__ import annotations

import tempfile
import time
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from autotrade.pipelines.fold_analysis import analysis_paths
from autotrade.pipelines.hitl_state import ANALYSIS_DIR_NAME, HITL_DIR_NAME, STATUS_NAME, read_json, read_status, repo_code_version

from . import equity, registry, steps
from .analysis import AnalysisService
from .manager import ExperimentManager, ManagerError, MAX_RUNNING_EXPERIMENTS
from .params_schema import parameter_schema
from .prompt_preview import build_prompt_preview
from .traces import read_trace_page, read_trace_tail, resolve_trace_path, stream_trace, trace_stats

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(repo_root: Path, experiments_root: Path | None = None) -> FastAPI:
    repo_root = Path(repo_root).resolve()
    analysis_service = AnalysisService(repo_root)
    # The manager must see the analysis service's pending work: its background
    # threads write into experiments/<id>/hitl/analysis/, so deletion is
    # refused (409) while an analysis for that experiment is still running.
    manager = ExperimentManager(
        repo_root, experiments_root, analysis_pending=analysis_service.pending_for_experiment
    )
    app = FastAPI(title="CornerHead Console", docs_url=None, redoc_url=None)
    trading_days_cache: dict[str, list[str] | None] = {}
    service_code_version = repo_code_version(repo_root)

    @app.middleware("http")
    async def revalidate_frontend_assets(request: Request, call_next):
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Keep clean, unversioned asset URLs and never retain stale UI
            # code in a browser or reverse-proxy cache.
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    def _trading_days() -> list[str]:
        # Loaded once per process (registry.clamped_trading_days does the
        # coverage clamping; None = no calendar, pickers degrade to text).
        if "days" not in trading_days_cache:
            trading_days_cache["days"] = registry.clamped_trading_days(repo_root)
        return trading_days_cache["days"] or []

    code_version_cache: dict[str, object] = {"at": 0.0, "value": ""}

    def _repo_code_version() -> str:
        """Current source fingerprint, 30s-cached for stale-process checks."""
        now = time.monotonic()
        if now - float(code_version_cache["at"]) > 30.0:
            code_version_cache["value"] = repo_code_version(repo_root)
            code_version_cache["at"] = now
        return str(code_version_cache["value"])

    def _code_status() -> dict[str, object]:
        current = _repo_code_version()
        return {
            "service_code_version": service_code_version,
            "repo_code_version": current,
            "code_current": service_code_version == current,
        }

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
            **_code_status(),
        }

    def _inherit_sources() -> list[str]:
        """Experiments with at least one recorded fold (inherit_from choices)."""
        root = manager.experiments_root
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and registry.latest_fold_records(registry.read_ledger_records(entry))
        )

    @app.get("/api/parameter-schema")
    def get_parameter_schema() -> dict[str, object]:
        return parameter_schema(trading_days=_trading_days(), inherit_sources=_inherit_sources())

    @app.get("/api/gpus")
    def get_gpus() -> dict[str, object]:
        """Live GPU inventory (nvidia-smi) for the pre-fold allocation picker."""
        try:
            from autotrade.environment.gpu import list_gpus

            return {"gpus": list_gpus()}
        except Exception as exc:  # noqa: BLE001 - a CPU-only host still gets a UI
            return {"gpus": [], "error": f"{type(exc).__name__}: {exc}"}

    # ---- experiments -----------------------------------------------------------
    @app.get("/api/experiments")
    def get_experiments() -> dict[str, object]:
        return {
            "experiments": registry.list_experiments(manager.experiments_root),
            "running": manager.running_experiments(),
            "max_running_experiments": MAX_RUNNING_EXPERIMENTS,
            **_code_status(),
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
        detail = registry.experiment_detail(manager.experiments_root, experiment_id)
        detail.update(_code_status())
        return detail

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
            **_code_status(),
        }

    # ---- zip downloads ---------------------------------------------------------
    def _zip_response(members: Iterable[tuple[Path, Path]], filename: str) -> FileResponse:
        """One-shot zip download: ``members`` yields (archive_name, file_path)
        pairs written into a temp archive that is unlinked after the response
        is sent — or immediately if archiving fails."""
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.close()
        zip_path = Path(handle.name)
        try:
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
                for archive_name, file in members:
                    archive.write(file, archive_name)
        except Exception:
            zip_path.unlink(missing_ok=True)  # archive failed: never orphan the temp file
            raise
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=filename,
            background=BackgroundTask(zip_path.unlink, missing_ok=True),
        )

    def _strategy_zip_response(
        strategy_dir: Path, model_dir: Path | None, filename: str
    ) -> FileResponse:
        """Frozen strategy package: output/ tree plus optional models/ tree."""

        def members() -> Iterator[tuple[Path, Path]]:
            for file in sorted(strategy_dir.rglob("*")):
                if file.is_file():
                    yield Path("output") / file.relative_to(strategy_dir), file
            if model_dir is not None and model_dir.is_dir():
                for file in sorted(model_dir.rglob("*")):
                    if file.is_file():
                        yield Path("models") / file.relative_to(model_dir), file

        return _zip_response(members(), filename)

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
        filename = f"{experiment_id}__{epoch_id}__{fold_id}.zip"
        return _strategy_zip_response(
            Path(str(strategy_dir)), Path(str(model_dir)) if model_dir else None, filename
        )

    # ---- step tree ---------------------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/steps")
    def get_step_tree(experiment_id: str) -> dict[str, object]:
        return steps.step_tree_view(_experiment_dir(experiment_id))

    @app.get("/api/experiments/{experiment_id}/steps/{node_id}/source.zip")
    def get_step_node_zip(experiment_id: str, node_id: str) -> FileResponse:
        experiment_dir = _experiment_dir(experiment_id)
        try:
            node_dir = steps.node_export_dir(experiment_dir, node_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _zip_response(
            (
                (file.relative_to(node_dir), file)
                for file in sorted(node_dir.rglob("*"))
                if file.is_file()
            ),
            f"{experiment_id}__{node_id}.zip",
        )

    def _current_step_snapshot(experiment_id: str) -> tuple[Path, dict[str, object], str, Path]:
        experiment_dir = _experiment_dir(experiment_id)
        status = read_status(experiment_dir / HITL_DIR_NAME / STATUS_NAME)
        if status.get("state") not in {"waiting_step_user", "waiting_user_reply"}:
            raise ValueError("experiment is not waiting for researcher input")
        run_id = str(status.get("run_id") or "")
        if not run_id or "/" in run_id or run_id.startswith("."):
            raise ValueError("live Step run is unavailable")
        # The console manager owns this per-experiment work root; constructing
        # the path here avoids trusting a mutable absolute path from status.json.
        steps_root = repo_root / ".runtime" / "sandboxes" / experiment_id / run_id / "artifacts" / "steps"
        node_id, node_dir = steps.current_node_export_dir(steps_root)
        return experiment_dir, status, node_id, node_dir

    @app.get("/api/experiments/{experiment_id}/current-step")
    def get_current_step(experiment_id: str) -> dict[str, object]:
        try:
            _experiment_dir, _status, node_id, _node_dir = _current_step_snapshot(experiment_id)
        except ValueError as exc:
            return {"available": False, "reason": str(exc)}
        return {"available": True, "node_id": node_id}

    @app.get("/api/experiments/{experiment_id}/current-step/source.zip")
    def get_current_step_zip(experiment_id: str) -> FileResponse:
        try:
            _experiment_dir, _status, node_id, node_dir = _current_step_snapshot(experiment_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        model_dir = node_dir / "models"
        return _strategy_zip_response(
            node_dir / "output",
            model_dir if model_dir.is_dir() else None,
            f"{experiment_id}__{node_id}.zip",
        )

    @app.get("/api/experiments/{experiment_id}/current-step/analysis")
    def get_current_step_analysis(experiment_id: str) -> dict[str, object]:
        try:
            experiment_dir, _status, node_id, _node_dir = _current_step_snapshot(experiment_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        md_path, meta_path = analysis_paths(
            experiment_dir / HITL_DIR_NAME / ANALYSIS_DIR_NAME, "step", node_id
        )
        return {
            "available": md_path.exists(),
            "pending": analysis_service.pending(experiment_id, "step", node_id),
            "content": md_path.read_text(encoding="utf-8") if md_path.exists() else None,
            "meta": read_json(meta_path) if meta_path.exists() else None,
        }

    @app.post("/api/experiments/{experiment_id}/current-step/analysis")
    def post_current_step_analysis(experiment_id: str) -> dict[str, object]:
        try:
            experiment_dir, status, node_id, node_dir = _current_step_snapshot(experiment_id)
            analysis_service.regenerate_step(
                experiment_dir=experiment_dir,
                experiment_id=experiment_id,
                node_id=node_id,
                node_dir=node_dir,
                status=status,
            )
        except (ManagerError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "started"}

    # ---- equity series ---------------------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/equity")
    def get_experiment_equity(experiment_id: str, epoch_id: str | None = Query(None)) -> dict[str, object]:
        _experiment_dir(experiment_id)
        return equity.experiment_equity_payload(manager.experiments_root, experiment_id, epoch_id=epoch_id)

    @app.get("/api/experiments/{experiment_id}/folds/{epoch_id}/{fold_id}/equity")
    def get_fold_equity(experiment_id: str, epoch_id: str, fold_id: str) -> dict[str, object]:
        _experiment_dir(experiment_id)
        try:
            return equity.fold_equity_payload(manager.experiments_root, experiment_id, epoch_id, fold_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ---- style analysis (Barra-lite) --------------------------------------------------
    @app.get("/api/experiments/{experiment_id}/style")
    def get_style_analysis(
        experiment_id: str, run_id: str = Query(...), prefix: str = Query(...)
    ) -> dict[str, object]:
        """Serve the run's persisted style rollup verbatim — the pipeline wrote
        it at replay time from frozen inputs; the web layer computes nothing."""
        experiment_dir = _experiment_dir(experiment_id)
        if prefix not in ("valid", "test", "heldout"):
            raise HTTPException(status_code=400, detail="prefix must be valid|test|heldout")
        if "/" in run_id or run_id.startswith("."):
            raise HTTPException(status_code=400, detail="invalid run_id")
        # Reveal gate: pre-reveal, sealed prefixes answer exactly like a
        # missing rollup so test/held-out existence never leaks.
        if prefix in registry.sealed_result_prefixes(experiment_dir):
            raise HTTPException(status_code=404, detail="该运行没有已落盘的风格归因结果")
        payload = read_json(experiment_dir / "artifacts" / run_id / "results" / f"style_{prefix}.json")
        if not payload:
            raise HTTPException(status_code=404, detail="该运行没有已落盘的风格归因结果")
        return payload

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
        """Assemble the session's system prompt for pre-approval review."""
        experiment_dir = _experiment_dir(experiment_id)
        try:
            return build_prompt_preview(
                experiment_dir,
                str(payload.get("session_key") or ""),
                str(payload.get("directive") or ""),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc.args[0] if exc.args else exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        tail_events: int | None = Query(None, ge=1, le=400),
    ) -> dict[str, object]:
        experiment_dir = _experiment_dir(experiment_id)
        path = resolve_trace_path(experiment_dir, run_id)
        if path is None:
            raise HTTPException(status_code=404, detail="no trace available for this run")
        page = read_trace_tail(path, max_events=tail_events) if tail_events is not None else read_trace_page(path, offset=offset)
        return {"trace_path": str(path), **page}

    @app.get("/api/experiments/{experiment_id}/trace/stream")
    def get_trace_stream(
        experiment_id: str,
        run_id: str | None = Query(None),
        offset: int = Query(0, ge=0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        experiment_dir = _experiment_dir(experiment_id)
        # Browser auto-reconnect echoes the last SSE id (byte offset), so a
        # dropped stream resumes near the tail instead of replaying from 0.
        if last_event_id and last_event_id.isdigit():
            offset = max(offset, int(last_event_id))
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
    # CAUTION: with SIGCHLD ignored, every subprocess.run() in THIS process
    # returns returncode 0 regardless of the child's real exit status. Current
    # callers (docker cleanup, nvidia-smi, git stamps) parse stdout or check
    # filesystem state only; new code here must not rely on returncode/check=True.
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    app = create_app(repo_root, experiments_root)
    if uds is not None:
        # Unix-socket bind: local access control is the parent directory's
        # filesystem permissions (loopback TCP is reachable by every local
        # user on a shared host). uvicorn chmods the socket itself to 666,
        # so the caller must keep the directory 0700.
        uvicorn.run(app, uds=str(uds), log_level="info", access_log=False)
    else:
        uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)


__all__ = ["create_app", "run"]

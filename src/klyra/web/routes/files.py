"""File serving, folder zip downloads, file uploads, and subject index routes for Klyra Web."""
from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from klyra.auth.dependencies import (
    can_access_subject,
    can_read_generated_file,
    get_current_user,
)
from klyra.storage import repo
from klyra.storage.models import UserRecord

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_DOCS_DIR = Path.cwd() / "docs"

_MIME_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
}

background_tasks_state: dict[str, dict] = {}


class BackgroundLogCapture(io.StringIO):
    def __init__(self, task_id: str, state_dict: dict):
        super().__init__()
        self.task_id = task_id
        self.state_dict = state_dict
        if "logs" not in self.state_dict[self.task_id]:
            self.state_dict[self.task_id]["logs"] = ""

    def write(self, string: str):
        sys.__stdout__.write(string)
        sys.__stdout__.flush()
        self.state_dict[self.task_id]["logs"] += string


@contextlib.contextmanager
def background_logger(task_id: str, state_dict: dict):
    capture = BackgroundLogCapture(task_id, state_dict)
    log_handler = logging.StreamHandler(capture)
    log_handler.setLevel(logging.INFO)
    log_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(log_handler)
    try:
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            yield
    finally:
        root_logger.removeHandler(log_handler)


class DeleteRequest(BaseModel):
    type: str
    items: list[str]


def _docs_dir() -> Path:
    """Return active docs directory, honoring monkeypatches on `klyra.web.server._DOCS_DIR`."""
    try:
        from klyra.web import server as _srv

        srv_docs = getattr(_srv, "_DOCS_DIR", None)
        if isinstance(srv_docs, Path) and srv_docs != _DEFAULT_DOCS_DIR:
            return srv_docs
    except Exception:
        pass
    cwd_docs = Path.cwd() / "docs"
    return cwd_docs if cwd_docs.exists() else _DEFAULT_DOCS_DIR


def _resolve_docs_file(path: str, user: UserRecord) -> tuple[Path | None, str]:
    """Resolve a path relative to `_docs_dir()`, handling 'assets/' vs '.assets/' and bare '.assets/'."""
    docs_base = _docs_dir()
    clean_path = path.lstrip("/")
    if clean_path.startswith("docs/"):
        clean_path = clean_path[len("docs/") :]

    candidates: list[str] = [clean_path]
    if "/assets/" in clean_path and "/.assets/" not in clean_path:
        candidates.append(clean_path.replace("/assets/", "/.assets/", 1))

    if clean_path.startswith(".assets/") or clean_path.startswith("assets/"):
        rest = clean_path.split("/", 1)[1] if "/" in clean_path else ""
        if rest and docs_base.exists():
            for subj_dir in sorted(docs_base.iterdir()):
                if subj_dir.is_dir() and not subj_dir.name.startswith("."):
                    if can_access_subject(user, subj_dir.name):
                        candidates.append(f"{subj_dir.name}/.assets/{rest}")

    docs_root = docs_base.resolve()
    for cand in candidates:
        fp = (docs_base / cand).resolve()
        if str(fp).startswith(str(docs_root)) and fp.exists() and fp.is_file():
            rel = fp.relative_to(docs_root).as_posix()
            return fp, rel

    fp = (docs_base / clean_path).resolve()
    return fp, clean_path


def _resolve_download_target(subject: str, folder: str) -> Path | None:
    """Resolve a folder or file inside `docs/<subject>/` for `/api/download/<subject>/<folder>`."""
    docs_base = _docs_dir()
    subj_root = (docs_base / subject).resolve()
    if not str(subj_root).startswith(str(docs_base.resolve())):
        return None

    clean = (folder or "").strip().lstrip("/")
    for prefix in (f"docs/{subject}/", f"{subject}/"):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :]

    raw_forms = [clean]
    if clean.lower().endswith(".zip"):
        raw_forms.append(clean[:-4])
    base_stem = Path(raw_forms[-1]).name
    if base_stem and base_stem not in raw_forms:
        raw_forms.append(base_stem)

    candidates: list[Path] = []
    for form in raw_forms:
        if not form:
            continue
        candidates.append(subj_root / form)
        if not form.startswith("generated/"):
            candidates.append(subj_root / "generated" / form)
            if not form.startswith("coding/"):
                candidates.append(subj_root / "generated" / "coding" / form)
            if not form.startswith("teaching/"):
                candidates.append(subj_root / "generated" / "teaching" / form)

    for cand in candidates:
        resolved = cand.resolve()
        if str(resolved).startswith(str(subj_root)) and resolved.exists():
            return resolved

    gen_root = (subj_root / "generated").resolve()
    if gen_root.exists():
        target_names = {Path(f).name.lower() for f in raw_forms if f}
        for p in sorted(gen_root.rglob("*")):
            if p.name.lower() in target_names and str(p.resolve()).startswith(str(subj_root)):
                return p.resolve()

    return (subj_root / clean).resolve()


def _collect_prefix_bundle_files(subject: str, folder: str) -> tuple[str, list[Path]]:
    """Find all generated files (and referenced diagram images) matching a QP/teaching/coding prefix
    such as `endterm-graph-qp` or `matrix-lab` when no single folder matches."""
    import re

    docs_base = _docs_dir().resolve()
    subj_root = (docs_base / subject).resolve()
    gen_root = (subj_root / "generated").resolve()
    if not gen_root.exists() or not str(subj_root).startswith(str(docs_base)):
        return "", []

    clean = (folder or "").strip().lstrip("/")
    if clean.lower().endswith(".zip"):
        clean = clean[:-4]
    prefix = Path(clean).name.lower()
    if not prefix:
        return "", []

    matched: list[Path] = []
    for p in sorted(gen_root.rglob("*")):
        if not p.is_file():
            continue
        pname = p.name.lower()
        if pname == prefix or pname.startswith(prefix + "-") or pname.startswith(prefix + "."):
            matched.append(p.resolve())
        elif prefix in [part.lower() for part in p.relative_to(gen_root).parts[:-1]]:
            matched.append(p.resolve())

    # Also include any diagram images referenced inside matched .md files
    img_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    for md_file in list(matched):
        if md_file.suffix.lower() != ".md":
            continue
        try:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for m in img_re.finditer(text):
            raw_img = m.group(1).strip()
            img_name = Path(raw_img).name
            if not img_name:
                continue
            for cand_img in (
                subj_root / ".assets" / "generated" / img_name,
                subj_root / ".assets" / img_name,
            ):
                if cand_img.exists() and cand_img.is_file() and cand_img.resolve() not in matched:
                    matched.append(cand_img.resolve())

    return prefix, matched


@router.get("/.assets/{rest:path}")
@router.get("/assets/{rest:path}")
async def serve_bare_asset(
    rest: str,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
):
    """Fallback route when Markdown contains relative '../.assets/...' links resolved against '/'."""
    return await serve_file(
        f".assets/{rest}",
        background_tasks=background_tasks,
        preview=False,
        download=False,
        user=user,
    )


@router.get("/files/{path:path}")
async def serve_file(
    path: str,
    background_tasks: BackgroundTasks,
    preview: bool = False,
    download: bool = False,
    user: UserRecord = Depends(get_current_user),
):
    """Serve generated files or assets for inline display, preview, or download (owner/course gated)."""
    docs_base = _docs_dir().resolve()
    file_path, rel_path = _resolve_docs_file(path, user)
    if file_path is None or not str(file_path).startswith(str(docs_base)):
        return Response(status_code=403)

    if not can_read_generated_file(user, rel_path):
        return Response(status_code=403, content="You do not have access to this file")

    if not file_path.is_file():
        parts = [p for p in rel_path.split("/") if p]
        if len(parts) >= 2:
            subj = parts[0]
            sub_folder = "/".join(parts[1:])
            return await download_folder(subj, sub_folder, background_tasks, user)
        return Response(status_code=404)

    suffix = file_path.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    media_type = _MIME_TYPES.get(suffix, "application/octet-stream")

    if preview and not is_image:
        try:
            content = file_path.read_text(encoding="utf-8")
            return Response(content=content, media_type="text/plain; charset=utf-8")
        except Exception:
            return Response(status_code=400, content="Cannot preview binary file")

    if download:
        return FileResponse(
            path=str(file_path),
            filename=file_path.name,
            media_type="application/octet-stream",
        )

    return FileResponse(path=str(file_path), media_type=media_type)


@router.get("/api/download/{subject}/{folder:path}")
async def download_folder(
    subject: str,
    folder: str,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
):
    """Zip and download a folder, or download a specific file (course + ownership gated)."""
    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")

    docs_base = _docs_dir().resolve()
    target_path = _resolve_download_target(subject, folder)

    if target_path is None or not str(target_path).startswith(str(docs_base)):
        return Response(status_code=403, content="Invalid path")

    if not target_path.exists():
        # Check if `folder` is a QP/teaching/coding prefix (e.g. `endterm-graph-qp` or `.assets/generated/endterm-graph-qp.zip`)
        bundle_prefix, bundle_files = _collect_prefix_bundle_files(subject, folder)
        if not bundle_files:
            return Response(status_code=404, content="File or folder not found")

        if user.role != "admin":
            for bf in bundle_files:
                rel = bf.relative_to(docs_base).as_posix()
                if not can_read_generated_file(user, rel):
                    return Response(status_code=403, content="Bundle contains files you cannot access")

        import zipfile

        fd, temp_zip_path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        subj_root = (docs_base / subject).resolve()
        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for bf in bundle_files:
                try:
                    arcname = bf.relative_to(subj_root / "generated").as_posix()
                except ValueError:
                    arcname = bf.relative_to(subj_root).as_posix()
                zf.write(bf, arcname=arcname)

        def cleanup_bundle():
            try:
                os.remove(temp_zip_path)
            except OSError:
                pass

        background_tasks.add_task(cleanup_bundle)
        return FileResponse(
            path=temp_zip_path,
            filename=f"{subject}_{bundle_prefix}.zip",
            media_type="application/zip",
        )

    if target_path.is_file():
        rel_path = target_path.relative_to(docs_base).as_posix()
        if not can_read_generated_file(user, rel_path):
            return Response(status_code=403, content="You do not have access to this file")
        return FileResponse(
            target_path,
            filename=target_path.name,
            media_type="application/octet-stream",
        )

    generated_root = (docs_base / subject / "generated").resolve()
    subj_root = (docs_base / subject).resolve()

    # Collect sibling <pkg>-*.md / .json / .tex / .pdf files when zipping a package subfolder inside generated/
    extra_sibling_files: list[Path] = []
    if str(target_path).startswith(str(generated_root)) and target_path.name.lower() not in {
        "generated",
        "coding",
        "teaching",
    }:
        pkg_prefix = target_path.name.lower()
        for sib in sorted(target_path.parent.iterdir()):
            if sib.is_file():
                sname = sib.name.lower()
                if sname.startswith(pkg_prefix + "-") or sname.startswith(pkg_prefix + "."):
                    extra_sibling_files.append(sib.resolve())

    if str(generated_root) in str(target_path) and user.role != "admin":
        for f in list(target_path.rglob("*")) + extra_sibling_files:
            if f.is_file():
                rel = f.relative_to(docs_base).as_posix()
                if not can_read_generated_file(user, rel):
                    return Response(status_code=403, content="Folder contains files you cannot access")

    fd, temp_zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    if extra_sibling_files:
        import zipfile

        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in sorted(target_path.rglob("*")):
                if f.is_file():
                    zf.write(f, arcname=f.relative_to(target_path).as_posix())
            for sib in extra_sibling_files:
                zf.write(sib, arcname=sib.name)
    else:
        base_name = temp_zip_path[:-4]
        shutil.make_archive(base_name, "zip", target_path)

    def cleanup():
        try:
            os.remove(temp_zip_path)
        except OSError:
            pass

    background_tasks.add_task(cleanup)
    safe_folder_name = target_path.name
    return FileResponse(
        path=temp_zip_path,
        filename=f"{subject}_{safe_folder_name}.zip",
        media_type="application/zip",
    )


@router.post("/api/upload/{subject}/{category}")
async def upload_files(
    subject: str,
    category: str,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
    files: list[UploadFile] = File(...),
):
    """Upload files and trigger background ingestion (course-gated)."""
    from klyra.services.ingestion_service import run_ingestion
    from klyra.utils.fs import validate_subject

    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")

    try:
        subject = validate_subject(subject)
    except ValueError as e:
        return Response(status_code=400, content=str(e))

    cat_path_map = {
        "pyq": "pyq",
        "transcripts": "transcripts",
        "additional": "additional",
    }
    dir_name = cat_path_map.get(category, category)
    target_dir = _docs_dir() / subject / dir_name
    target_dir.mkdir(parents=True, exist_ok=True)

    task_id = str(uuid.uuid4())
    background_tasks_state[task_id] = {
        "status": "running",
        "progress": 0,
        "message": "Starting ingestion...",
        "logs": "",
    }

    files_copied = []
    try:
        for upload_file in files:
            if upload_file.filename:
                file_path = target_dir / upload_file.filename
                with open(file_path, "wb") as buffer:
                    shutil.copyfileobj(upload_file.file, buffer)
                files_copied.append(file_path)
    except Exception as e:
        return Response(status_code=500, content=f"Upload failed: {e}")

    def ingest_background():
        with background_logger(task_id, background_tasks_state):
            try:
                kwargs = {}
                if category.startswith("week_"):
                    kwargs["week"] = int(category.split("_")[1])
                else:
                    kwargs["categories"] = [category]
                run_ingestion(subject, **kwargs)
                background_tasks_state[task_id] = {
                    "status": "completed",
                    "progress": 100,
                    "message": "Indexing complete",
                    "logs": background_tasks_state[task_id].get("logs", ""),
                }
            except Exception as e:
                logger.error(f"Ingestion failed for {subject}/{category}: {e}")
                background_tasks_state[task_id] = {
                    "status": "error",
                    "progress": 0,
                    "message": f"Ingestion failed: {e}",
                    "logs": background_tasks_state[task_id].get("logs", ""),
                }
                for f in files_copied:
                    try:
                        f.unlink(missing_ok=True)
                    except Exception:
                        pass
                if not any(target_dir.iterdir()):
                    try:
                        target_dir.rmdir()
                    except Exception:
                        pass

    background_tasks.add_task(ingest_background)
    return {"message": "Files uploaded successfully. Indexing started.", "task_id": task_id}


@router.get("/api/tasks/{task_id}")
async def get_task_status(task_id: str, user: UserRecord = Depends(get_current_user)):
    return background_tasks_state.get(task_id, {"status": "unknown"})


@router.get("/api/subjects")
def get_subjects(user: UserRecord = Depends(get_current_user)):
    """Return the subjects the user may work with (admin: all on disk)."""
    docs_base = _docs_dir()
    if user.role == "admin":
        subjects = []
        if docs_base.exists():
            for p in docs_base.iterdir():
                if p.is_dir() and not p.name.startswith("."):
                    subjects.append(p.name)
        return {"subjects": sorted(subjects)}
    return {"subjects": sorted(repo.user_courses(user))}


@router.get("/api/index/{subject}")
async def list_index(subject: str, user: UserRecord = Depends(get_current_user)):
    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")
    from klyra.utils.fs import discover_subject_files

    try:
        sources = discover_subject_files(subject)
    except FileNotFoundError:
        return {"folders": [], "files": []}

    folders = set()
    files = []
    source_dir = Path("docs") / subject
    for s in sources:
        files.append(str(s.path.resolve()))
        try:
            rel = s.path.relative_to(source_dir)
            if len(rel.parts) > 1:
                folders.add(rel.parts[0])
        except ValueError as e:
            logger.error(f"ValueError relative_to. s.path={s.path}, source_dir={source_dir}: {e}")

    return {"folders": sorted(list(folders)), "files": sorted(files)}


@router.post("/api/index/{subject}/delete")
async def delete_index(
    subject: str,
    req: DeleteRequest,
    background_tasks: BackgroundTasks,
    user: UserRecord = Depends(get_current_user),
):
    if not can_access_subject(user, subject):
        return Response(status_code=403, content="You are not assigned to this course")
    from klyra.services.ingestion_service import run_ingestion
    from klyra.utils.fs import discover_subject_files

    files_to_delete = []
    if req.type == "folders":
        try:
            sources = discover_subject_files(subject)
            source_dir = Path("docs") / subject
            for s in sources:
                try:
                    rel = s.path.relative_to(source_dir)
                    if len(rel.parts) > 0 and rel.parts[0] in req.items:
                        files_to_delete.append(str(s.path.resolve()))
                except ValueError:
                    pass
        except FileNotFoundError:
            pass
    else:
        files_to_delete = req.items

    if not files_to_delete:
        return {"message": "No files found to delete"}

    task_id = str(uuid.uuid4())
    total_files = len(files_to_delete)
    background_tasks_state[task_id] = {
        "status": "running",
        "progress": 0,
        "message": "Starting deletion...",
        "logs": "",
    }
    docs_base = _docs_dir()

    def delete_background():
        with background_logger(task_id, background_tasks_state):
            for i, file_path in enumerate(files_to_delete):
                try:
                    run_ingestion(subject, delete_file=file_path)
                    try:
                        p = Path(file_path)
                        if p.exists() and str(p.resolve()).startswith(str(docs_base.resolve())):
                            p.unlink()
                            parent = p.parent
                            subject_dir_resolved = (docs_base / subject).resolve()
                            while parent != subject_dir_resolved:
                                if not any(parent.iterdir()):
                                    parent.rmdir()
                                    parent = parent.parent
                                else:
                                    break
                    except Exception as e:
                        logger.error(f"Failed to delete physical file {file_path}: {e}")
                except Exception as e:
                    logger.error(f"Failed to delete {file_path} from index: {e}")

                pct = int(((i + 1) / total_files) * 100)
                background_tasks_state[task_id] = {
                    "status": "running",
                    "progress": pct,
                    "message": f"Deleting {i+1}/{total_files}...",
                    "logs": background_tasks_state[task_id].get("logs", ""),
                }

            background_tasks_state[task_id] = {
                "status": "completed",
                "progress": 100,
                "message": "Deletion complete",
                "logs": background_tasks_state[task_id].get("logs", ""),
            }

    background_tasks.add_task(delete_background)
    return {"message": f"Started deletion of {len(files_to_delete)} files", "task_id": task_id}


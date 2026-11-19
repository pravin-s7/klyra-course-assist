"""File serving, folder zip downloads, file uploads, and subject index routes for Klyra Web."""
from __future__ import annotations

import contextlib
from datetime import datetime, timezone
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
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
    ".md": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".py": "text/plain; charset=utf-8",
    ".java": "text/plain; charset=utf-8",
    ".c": "text/plain; charset=utf-8",
    ".cpp": "text/plain; charset=utf-8",
    ".h": "text/plain; charset=utf-8",
    ".sh": "text/plain; charset=utf-8",
    ".bash": "text/plain; charset=utf-8",
    ".sed": "text/plain; charset=utf-8",
    ".awk": "text/plain; charset=utf-8",
    ".sql": "text/plain; charset=utf-8",
    ".csv": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".js": "text/plain; charset=utf-8",
    ".ts": "text/plain; charset=utf-8",
    ".css": "text/plain; charset=utf-8",
    ".html": "text/plain; charset=utf-8",
    ".xml": "text/plain; charset=utf-8",
    ".toml": "text/plain; charset=utf-8",
    ".ini": "text/plain; charset=utf-8",
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

    return FileResponse(
        path=str(file_path),
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{file_path.name}"'},
    )


@router.get("/preview/{path:path}")
async def preview_file_page(
    path: str,
    user: UserRecord = Depends(get_current_user),
):
    """Render an in-browser preview page for any file in `docs/` when opened in a new tab."""
    import html as _html
    import json as _json

    docs_base = _docs_dir().resolve()
    file_path, rel_path = _resolve_docs_file(path, user)
    if file_path is None or not str(file_path).startswith(str(docs_base)):
        return HTMLResponse("<h3>403 Forbidden: Invalid file path</h3>", status_code=403)

    if not can_read_generated_file(user, rel_path):
        return HTMLResponse(
            "<h3>403 Forbidden: You do not have permission to view this file.</h3>",
            status_code=403,
        )

    if not file_path.is_file():
        return HTMLResponse("<h3>404 Not Found</h3>", status_code=404)

    suffix = file_path.suffix.lower()
    is_image = suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
    is_pdf = suffix == ".pdf"
    is_markdown = suffix == ".md"
    file_url = f"/files/{rel_path}"
    download_url = f"/files/{rel_path}?download=true"
    safe_name = _html.escape(file_path.name)
    safe_rel = _html.escape(rel_path)
    try:
        size_bytes = file_path.stat().st_size
    except OSError:
        size_bytes = 0

    text_content: str | None = None
    if not is_image and not is_pdf:
        if suffix == ".docx":
            try:
                from klyra.chat import ChatOrchestrator

                text_content = ChatOrchestrator._extract_docx_text(file_path, max_chars=200000)
            except Exception:
                text_content = None
        else:
            try:
                text_content = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text_content = None

    if is_pdf:
        body_html = f'<iframe src="{_html.escape(file_url)}" style="display:block;width:100%;height:calc(100vh - 52px);border:none;background:#111;"></iframe>'
    elif is_image:
        body_html = f"""
        <div style="display:flex;align-items:center;justify-content:center;min-height:calc(100vh - 70px);padding:24px;">
            <img src="{_html.escape(file_url)}" alt="{safe_name}" style="max-width:95vw;max-height:85vh;object-fit:contain;border-radius:8px;border:1px solid var(--border);background:#fff;padding:8px;">
        </div>
        """
    elif text_content is not None and is_markdown:
        json_text = _json.dumps(text_content)
        json_url = _json.dumps(file_url)
        body_html = f"""
        <div style="max-width:980px;margin:24px auto;padding:0 20px 60px;">
            <div id="mdRendered" class="doc-preview-content markdown-rendered" style="background:var(--bg-tertiary);border:1px solid var(--border);border-radius:12px;padding:28px 32px;max-height:none;overflow:visible;"></div>
            <pre id="mdRaw" style="display:none;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:12px;padding:24px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-size:13px;line-height:1.6;">{_html.escape(text_content)}</pre>
        </div>
        <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
        <script>
            MathJax = {{
                tex: {{ inlineMath: [['$', '$'], ['\\\\(', '\\\\)']], displayMath: [['$$', '$$'], ['\\\\[', '\\\\]']] }}
            }};
        </script>
        <script id="MathJax-script" async src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>
        <script src="/static/markdown.js?v=5"></script>
        <script>
            const rawMd = {json_text};
            const srcUrl = {json_url};
            const renderedEl = document.getElementById('mdRendered');
            const rawEl = document.getElementById('mdRaw');
            renderedEl.innerHTML = renderMarkdown(rawMd, srcUrl);
            window.addEventListener('load', () => {{
                if (window.MathJax && window.MathJax.typesetPromise) {{
                    MathJax.typesetPromise([renderedEl]).catch(() => {{}});
                }}
            }});
            function toggleRawMd(btn) {{
                const showingRaw = rawEl.style.display !== 'none';
                rawEl.style.display = showingRaw ? 'none' : 'block';
                renderedEl.style.display = showingRaw ? 'block' : 'none';
                btn.textContent = showingRaw ? 'View Raw' : 'View Rendered';
            }}
            function copyFileText(btn) {{
                navigator.clipboard.writeText(rawMd).then(() => {{
                    const old = btn.textContent;
                    btn.textContent = 'Copied ✓';
                    setTimeout(() => btn.textContent = old, 1500);
                }});
            }}
        </script>
        """
    elif text_content is not None:
        json_text = _json.dumps(text_content)
        body_html = f"""
        <div style="max-width:1100px;margin:20px auto;padding:0 20px 60px;">
            <pre style="background:var(--bg-tertiary);border:1px solid var(--border);border-radius:10px;padding:20px 24px;overflow-x:auto;white-space:pre-wrap;word-break:break-word;font-family:monospace;font-size:13px;line-height:1.55;">{_html.escape(text_content)}</pre>
        </div>
        <script>
            const rawText = {json_text};
            function copyFileText(btn) {{
                navigator.clipboard.writeText(rawText).then(() => {{
                    const old = btn.textContent;
                    btn.textContent = 'Copied ✓';
                    setTimeout(() => btn.textContent = old, 1500);
                }});
            }}
        </script>
        """
    else:
        body_html = f"""
        <div style="max-width:520px;margin:80px auto;text-align:center;background:var(--bg-tertiary);border:1px solid var(--border);border-radius:12px;padding:32px;">
            <h2 style="margin-bottom:8px;">{safe_name}</h2>
            <p style="color:var(--text-muted);margin-bottom:20px;">Binary file ({size_bytes:,} bytes) — download to open locally.</p>
            <a href="{_html.escape(download_url)}" download="{safe_name}" class="btn primary-btn">Download File</a>
        </div>
        """

    extra_buttons = ""
    if text_content is not None:
        if is_markdown:
            extra_buttons += '<button type="button" class="btn" style="padding:5px 10px;font-size:12px;" onclick="toggleRawMd(this)">View Raw</button>'
        extra_buttons += '<button type="button" class="btn" style="padding:5px 10px;font-size:12px;" onclick="copyFileText(this)">Copy</button>'

    page = f"""<!DOCTYPE html>
<html lang="en" style="height:auto;overflow-y:auto;">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Preview · {safe_name}</title>
    <link rel="stylesheet" href="/static/style.css?v=12">
    <script>
        const savedTheme = localStorage.getItem('klyra_theme') || 'dark';
        document.documentElement.setAttribute('data-theme', savedTheme);
    </script>
</head>
<body style="display:block;overflow:visible;height:auto;min-height:100vh;padding-top:52px;">
    <header style="position:fixed;top:0;left:0;right:0;height:52px;z-index:30;display:flex;align-items:center;justify-content:space-between;padding:8px 20px;background:var(--bg-tertiary);border-bottom:1px solid var(--border);">
        <div style="display:flex;align-items:center;gap:10px;min-width:0;">
            <span style="color:var(--accent);font-weight:700;">✦ Klyra Preview</span>
            <code style="font-size:12.5px;color:var(--text-secondary);background:var(--bg-hover);padding:2px 8px;border-radius:6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{safe_rel}</code>
            <span style="font-size:11.5px;color:var(--text-muted);">({size_bytes:,} bytes)</span>
        </div>
        <div style="display:flex;align-items:center;gap:8px;flex-shrink:0;">
            {extra_buttons}
            <a href="{_html.escape(download_url)}" download="{safe_name}" class="btn primary-btn" style="padding:5px 12px;font-size:12px;">Download</a>
        </div>
    </header>
    {body_html}
</body>
</html>"""
    return HTMLResponse(page)


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


_CHAT_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
_CHAT_FILE_EXTS = {
    ".pdf", ".txt", ".md", ".doc", ".docx", ".csv", ".json", ".tex",
    ".py", ".c", ".cpp", ".h", ".java", ".js", ".ts", ".sql", ".sh",
    ".bash", ".yaml", ".yml", ".xml", ".html", ".css", ".pptx", ".xlsx", ".rtf",
}
_CHAT_ALLOWED_EXTS = _CHAT_IMAGE_EXTS | _CHAT_FILE_EXTS
_CONTENT_TYPE_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
_MAX_CHAT_ARTIFACT_BYTES = 25 * 1024 * 1024
_IGNORED_GENERATED_EXTS = {".aux", ".log", ".out", ".toc", ".fls", ".fdb_latexmk", ".synctex.gz"}


def _safe_artifact_filename(raw_name: str, fallback_ext: str = ".png") -> str:
    """Return a filesystem-safe basename preserving allowed extension."""
    base = Path(raw_name or f"artifact{fallback_ext}").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(base).stem).strip("._") or "artifact"
    ext = Path(base).suffix.lower()
    if ext not in _CHAT_ALLOWED_EXTS:
        ext = fallback_ext
    return f"{stem[:80]}{ext}"


def _file_mtime_iso(fp: Path) -> str:
    try:
        return datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return datetime.now(timezone.utc).isoformat()


def _artifact_source_for_rel(rel_path: str) -> str:
    if rel_path.startswith(".chat_uploads/") or "/generated/uploads/" in rel_path:
        return "uploaded"
    return "generated"


def _strip_upload_uuid_prefix(filename: str) -> str:
    """Strip leading 8-hex-char upload prefix `<hex8>_<name>` for clean display."""
    if len(filename) > 9 and filename[8] == "_" and all(c in "0123456789abcdef" for c in filename[:8]):
        return filename[9:]
    return filename


@router.post("/api/chat/artifacts/upload")
async def upload_chat_artifact(
    file: UploadFile = File(...),
    subject: str = Form(""),
    session_id: str = Form(""),
    user: UserRecord = Depends(get_current_user),
):
    """Upload an image or document artifact from the chat composer and record ownership."""
    raw_name = file.filename or "artifact"
    ext = Path(raw_name).suffix.lower()
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if ext not in _CHAT_ALLOWED_EXTS:
        ext = _CONTENT_TYPE_TO_EXT.get(content_type, "")
    if ext not in _CHAT_ALLOWED_EXTS:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file type. Attach images (PNG, JPG, GIF, WEBP, SVG) or documents (PDF, TXT, MD, DOC, DOCX, CSV, JSON, TEX, code).",
        )

    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(data) > _MAX_CHAT_ARTIFACT_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds maximum allowed size of 25 MB.")

    if user.role != "admin" and not repo.user_courses(user):
        raise HTTPException(
            status_code=403,
            detail="You have no assigned subjects. Ask an administrator to assign your courses.",
        )

    subject_clean = subject.strip()
    if subject_clean and not can_access_subject(user, subject_clean):
        raise HTTPException(status_code=403, detail="You are not assigned to this subject.")

    safe_name = _safe_artifact_filename(raw_name, fallback_ext=ext)
    short_id = uuid.uuid4().hex[:8]
    stored_name = f"{short_id}_{safe_name}"
    docs_base = _docs_dir()

    if subject_clean:
        target_dir = docs_base / subject_clean / "generated" / "uploads"
        rel_path = f"{subject_clean}/generated/uploads/{stored_name}"
        owner_subject = subject_clean
    else:
        target_dir = docs_base / ".chat_uploads" / user.id
        rel_path = f".chat_uploads/{user.id}/{stored_name}"
        owner_subject = "_general"

    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / stored_name
    target_path.write_bytes(data)
    repo.set_file_owner(rel_path, user.id, owner_subject)

    is_image = ext in _CHAT_IMAGE_EXTS
    mime_type = _MIME_TYPES.get(ext, content_type or "application/octet-stream")
    now_iso = datetime.now(timezone.utc).isoformat()
    logger.info(
        "Chat artifact uploaded by @%s: %s (subject=%s, session=%s, %d bytes)",
        user.username,
        rel_path,
        subject_clean or "general",
        session_id.strip() or "-",
        len(data),
    )
    return {
        "name": safe_name,
        "rel_path": rel_path,
        "url": f"/files/{rel_path}",
        "preview_url": f"/preview/{rel_path}",
        "download_url": f"/files/{rel_path}",
        "file_type": ext,
        "mime_type": mime_type,
        "size": len(data),
        "subject": subject_clean,
        "session_id": session_id.strip(),
        "source": "uploaded",
        "is_image": is_image,
        "category": "image" if is_image else "file",
        "created_at": now_iso,
    }


def collect_user_artifacts(
    target_user: UserRecord,
    *,
    strict_owner_only: bool = False,
) -> dict:
    """Collect uploaded and generated artifacts for ``target_user``.

    When ``strict_owner_only`` is False (default, used by ``/api/artifacts``),
    returns all artifacts visible to ``target_user`` across their sessions and
    accessible subjects. When ``strict_owner_only`` is True (used by the Admin
    Panel's per-user artifacts view), returns only artifacts belonging directly
    to ``target_user`` (their own chat sessions, DB-owned files, and personal
    ``.chat_uploads/<user_id>`` folder).
    """
    from klyra.web.routes.sessions import ACTIVE_SESSIONS

    docs_base = _docs_dir()
    docs_root = docs_base.resolve()
    artifacts_by_key: dict[str, dict] = {}
    chats_with_counts: dict[str, dict] = {}

    owned_records = repo.list_files_by_owner(target_user.id)
    owned_by_rel = {r.rel_path: r for r in owned_records}

    def _register_artifact(entry: dict) -> None:
        key = entry["key"]
        existing = artifacts_by_key.get(key)
        if existing is None:
            artifacts_by_key[key] = entry
        else:
            if not existing.get("session_id") and entry.get("session_id"):
                existing["session_id"] = entry["session_id"]
                existing["session_title"] = entry["session_title"]
            if entry.get("timestamp", "") > existing.get("timestamp", ""):
                existing["timestamp"] = entry["timestamp"]
            if entry.get("owned"):
                existing["owned"] = True

    def _add_file_artifact(
        rel_raw: str,
        display_name: str = "",
        source_hint: str = "",
        subject_hint: str = "",
        session_id: str = "",
        session_title: str = "",
        timestamp: str = "",
    ) -> None:
        fp, rel_path = _resolve_docs_file(rel_raw, target_user)
        if not fp or not fp.exists() or not fp.is_file():
            return
        if not strict_owner_only and not can_read_generated_file(target_user, rel_path):
            return
        ext = fp.suffix.lower()
        if ext in _IGNORED_GENERATED_EXTS or fp.name.startswith("."):
            return
        parts = rel_path.split("/")
        inferred_subject = "" if parts[0] == ".chat_uploads" else parts[0]
        subject_val = (subject_hint or inferred_subject).strip()
        if subject_val == "_general":
            subject_val = ""
        source_val = source_hint or _artifact_source_for_rel(rel_path)
        name_val = display_name or (
            _strip_upload_uuid_prefix(fp.name) if source_val == "uploaded" else fp.name
        )
        owned_rec = owned_by_rel.get(rel_path)
        ts_val = timestamp or (owned_rec.created_at if owned_rec else "") or _file_mtime_iso(fp)
        try:
            size_val = fp.stat().st_size
        except OSError:
            size_val = 0
        _register_artifact(
            {
                "key": rel_path,
                "name": name_val,
                "rel_path": rel_path,
                "url": f"/files/{rel_path}",
                "preview_url": f"/preview/{rel_path}",
                "download_url": f"/files/{rel_path}",
                "file_type": ext,
                "mime_type": _MIME_TYPES.get(ext, "application/octet-stream"),
                "is_image": ext in _CHAT_IMAGE_EXTS,
                "category": "image" if ext in _CHAT_IMAGE_EXTS else "file",
                "size": size_val,
                "source": source_val,
                "subject": subject_val,
                "session_id": session_id,
                "session_title": session_title,
                "timestamp": ts_val,
                "owned": owned_rec is not None,
                "exists": True,
            }
        )

    # 1. Scan chat sessions
    session_owner_filter = (
        target_user.id
        if strict_owner_only or target_user.role != "admin"
        else None
    )
    db_sessions = repo.list_chat_sessions(owner_id=session_owner_filter)
    for rec in db_sessions:
        sid = rec.session_id
        title = rec.title or "Untitled Chat"
        history: list = []
        active = ACTIVE_SESSIONS.get(sid)
        if active and active.get("history"):
            history = list(active["history"])
        elif isinstance(rec.ui_history_json, list):
            history = list(rec.ui_history_json)
        elif isinstance(rec.ui_history_json, str) and rec.ui_history_json:
            try:
                parsed = json.loads(rec.ui_history_json)
                if isinstance(parsed, list):
                    history = parsed
            except json.JSONDecodeError:
                history = []

        for ev in history:
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type", "")
            ev_ts = str(ev.get("timestamp") or rec.modified_at or rec.created_at or "")
            if ev_type == "user_transcript" and isinstance(ev.get("attachments"), list):
                for att in ev["attachments"]:
                    if not isinstance(att, dict):
                        continue
                    rel_raw = str(att.get("rel_path") or "").strip()
                    if not rel_raw and str(att.get("url", "")).startswith("/files/"):
                        rel_raw = str(att["url"])[len("/files/") :]
                    if not rel_raw:
                        continue
                    _add_file_artifact(
                        rel_raw=rel_raw,
                        display_name=str(att.get("name") or ""),
                        source_hint="uploaded",
                        subject_hint=str(att.get("subject") or ""),
                        session_id=sid,
                        session_title=title,
                        timestamp=ev_ts,
                    )
            elif ev_type == "file_link":
                url = str(ev.get("url") or "").strip()
                name = str(ev.get("name") or "").strip()
                if url.startswith("/files/"):
                    _add_file_artifact(
                        rel_raw=url[len("/files/") :],
                        display_name=name,
                        session_id=sid,
                        session_title=title,
                        timestamp=ev_ts,
                    )
                elif url.startswith("/api/download/"):
                    tail = url[len("/api/download/") :]
                    subj = tail.split("/", 1)[0] if "/" in tail else ""
                    if subj and (strict_owner_only or can_access_subject(target_user, subj)):
                        _register_artifact(
                            {
                                "key": url,
                                "name": name or Path(tail).name or "bundle.zip",
                                "rel_path": tail,
                                "url": url,
                                "preview_url": "",
                                "download_url": url,
                                "file_type": ".zip",
                                "mime_type": "application/zip",
                                "is_image": False,
                                "category": "file",
                                "size": 0,
                                "source": "generated",
                                "subject": subj,
                                "session_id": sid,
                                "session_title": title,
                                "timestamp": ev_ts,
                                "owned": False,
                                "exists": True,
                            }
                        )

    # 2. Include DB-owned files & disk files
    accessible_subjects: list[str] = []
    if strict_owner_only:
        for rec in owned_records:
            subj_val = "" if rec.subject == "_general" else (rec.subject or "")
            _add_file_artifact(
                rel_raw=rec.rel_path,
                subject_hint=subj_val,
                timestamp=rec.created_at or "",
            )
        accessible_subjects = sorted(repo.user_courses(target_user))
    else:
        if target_user.role == "admin":
            if docs_base.exists():
                accessible_subjects = sorted(
                    p.name for p in docs_base.iterdir() if p.is_dir() and not p.name.startswith(".")
                )
        else:
            accessible_subjects = sorted(repo.user_courses(target_user))

        for subj in accessible_subjects:
            for scan_root in (docs_base / subj / "generated", docs_base / subj / ".assets" / "generated"):
                if not scan_root.exists():
                    continue
                for fp in sorted(scan_root.rglob("*")):
                    if not fp.is_file():
                        continue
                    try:
                        rel_path = fp.resolve().relative_to(docs_root).as_posix()
                    except ValueError:
                        continue
                    if rel_path not in artifacts_by_key:
                        _add_file_artifact(rel_raw=rel_path, subject_hint=subj)

    uploads_base = docs_base / ".chat_uploads"
    if uploads_base.exists():
        scan_dirs = (
            [p for p in uploads_base.iterdir() if p.is_dir()]
            if (not strict_owner_only and target_user.role == "admin")
            else [uploads_base / target_user.id]
        )
        for udir in scan_dirs:
            if not udir.exists() or not udir.is_dir():
                continue
            for fp in sorted(udir.rglob("*")):
                if not fp.is_file():
                    continue
                try:
                    rel_path = fp.resolve().relative_to(docs_root).as_posix()
                except ValueError:
                    continue
                if rel_path not in artifacts_by_key:
                    _add_file_artifact(rel_raw=rel_path, source_hint="uploaded")

    artifacts = sorted(
        artifacts_by_key.values(),
        key=lambda item: item.get("timestamp") or "",
        reverse=True,
    )

    subjects_set = set(accessible_subjects)
    for item in artifacts:
        if item.get("subject"):
            subjects_set.add(item["subject"])
        sid = item.get("session_id") or ""
        if sid:
            entry = chats_with_counts.setdefault(
                sid,
                {
                    "id": sid,
                    "title": item.get("session_title") or "Untitled Chat",
                    "count": 0,
                    "latest": item.get("timestamp") or "",
                },
            )
            entry["count"] += 1
            if (item.get("timestamp") or "") > entry["latest"]:
                entry["latest"] = item["timestamp"]

    chats = sorted(chats_with_counts.values(), key=lambda c: c.get("latest") or "", reverse=True)
    uploaded_count = sum(1 for a in artifacts if a.get("source") == "uploaded")
    generated_count = sum(1 for a in artifacts if a.get("source") != "uploaded")
    return {
        "artifacts": artifacts,
        "subjects": sorted(subjects_set),
        "chats": chats,
        "uploaded_count": uploaded_count,
        "generated_count": generated_count,
    }


@router.get("/api/artifacts")
async def list_artifacts(user: UserRecord = Depends(get_current_user)):
    """List user-uploaded and Klyra-generated artifacts visible to the user, linked to subjects & chats."""
    return collect_user_artifacts(user, strict_owner_only=False)



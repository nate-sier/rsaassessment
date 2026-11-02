"""Backup and restore local STATSports result folders without exposing credentials.

The backup contains athlete GPS caches and reviewed results. Treat it as private.
"""
from __future__ import annotations
import io
import os
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

MAX_UNCOMPRESSED = 850 * 1024 * 1024
MAX_FILES = 7000
ALLOWED_EXTS = {'.sqlite', '.csv', '.json', '.html', '.pdf', '.jsonl', '.txt'}
IGNORED_NAMES = {'secrets.toml', '.env', '.DS_Store'}


def default_locations(project: Path) -> tuple[Path, Path]:
    """Use existing Mac Downloads layout; use ignored local workspace in the cloud."""
    local = Path.home() / 'Downloads'
    is_cloud = os.getenv('STATSPORTS_CLOUD_MODE') == '1' or not local.is_dir()
    default_data = (project / '.runtime_data' / 'automated' if is_cloud
                    else local / 'statsports_all_drills_and_assessments_v22')
    default_review = (project / '.runtime_data' / 'reviewed' if is_cloud
                      else local / 'statsports_manually_reviewed_v29')
    return (Path(os.getenv('STATSPORTS_DATA_ROOT', str(default_data))).expanduser().resolve(),
            Path(os.getenv('STATSPORTS_REVIEW_ROOT', str(default_review))).expanduser().resolve())


def _allowed(p: Path) -> bool:
    return (p.suffix.lower() in ALLOWED_EXTS and p.name not in IGNORED_NAMES and
            not p.is_symlink() and p.is_file())


def backup_workspace(data_root: Path, review_root: Path) -> bytes:
    """Package current work; does NOT include API credentials or local secrets."""
    out = io.BytesIO()
    count = 0
    total = 0
    with zipfile.ZipFile(out, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=5) as z:
        for label, root in (('automated', data_root), ('reviewed', review_root)):
            if not root.exists():
                continue
            for p in sorted(root.rglob('*')):
                if not _allowed(p):
                    continue
                relative = p.relative_to(root)
                # Avoid unrelated arbitrary directories and temporary archives.
                if not relative.parts or not relative.parts[0].startswith('statsports_all_assessments_'):
                    continue
                count += 1
                total += p.stat().st_size
                if count > MAX_FILES or total > MAX_UNCOMPRESSED:
                    raise ValueError('Workspace is too large for in-browser backup; use secure offline file transfer instead.')
                z.write(p, arcname=(PurePosixPath(label) / PurePosixPath(*relative.parts)).as_posix())
    return out.getvalue()


def restore_workspace(blob: bytes, data_root: Path, review_root: Path) -> int:
    """Safely restore only recognized workspace files, never arbitrary paths.

    Existing files are preserved to avoid silently overwriting newer edits.
    """
    count = 0
    total = 0
    prepared = []
    archive_targets = set()
    with zipfile.ZipFile(io.BytesIO(blob), 'r') as z:
        for info in z.infolist():
            if info.is_dir():
                continue
            name = info.filename
            p = PurePosixPath(name)
            if (p.is_absolute() or '\\' in name or len(p.parts) < 3 or
                '..' in p.parts or p.parts[0] not in ('automated', 'reviewed')):
                raise ValueError('Backup contains an unsafe or unsupported file path.')
            if not p.parts[1].startswith('statsports_all_assessments_') or p.suffix.lower() not in ALLOWED_EXTS or p.name in IGNORED_NAMES:
                raise ValueError('Backup contains a file outside an assessment workspace.')
            if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
                raise ValueError('Backup contains a symlink; restore blocked.')
            if info.file_size > 120 * 1024 * 1024:
                raise ValueError('Individual backup file exceeds safety limit.')
            total += info.file_size
            count += 1
            if count > MAX_FILES or total > MAX_UNCOMPRESSED:
                raise ValueError('Backup exceeds safety limit.')
            root = data_root if p.parts[0] == 'automated' else review_root
            target = root.joinpath(*p.parts[1:])
            if not target.resolve().is_relative_to(root.resolve()):
                raise ValueError('Backup target escaped workspace.')
            if target in archive_targets:
                raise ValueError('Backup includes duplicate target entries.')
            archive_targets.add(target)
            if target.exists():
                # Never overwrite manually reviewed corrections or the raw source.
                raise FileExistsError(f'Restore would overwrite existing file: {p.as_posix()}. Back up and clear the workspace first.')
            prepared.append((info, target))
        # Read all members and validate before writing anything.
        payloads = [(target, z.read(info)) for info, target in prepared]
    for target, payload in payloads:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return len(payloads)

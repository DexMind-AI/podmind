"""On-disk transcript VTT format: plain ``transcript.vtt`` or xz-compressed
``transcript.vtt.xz``.

VTT (timestamped) transcripts are highly redundant and read by nothing in the
pipeline after the cascade derives ``transcript.md`` once at write time, so they
are stored xz-compressed by default. The plain-text ``transcript.md`` (the
summarizer's input and the grep target) is unaffected and lives outside this
module.

Compression is on by default; set ``PODMIND_COMPRESS_TRANSCRIPTS=0`` to disable.
"""
from __future__ import annotations

import lzma
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

PLAIN = "transcript.vtt"
XZ = "transcript.vtt.xz"

_FALSEY = {"0", "false", "no", "off"}


def should_compress() -> bool:
    """Default True; an unset or empty env var counts as unset (→ True)."""
    val = os.environ.get("PODMIND_COMPRESS_TRANSCRIPTS")
    if not val:
        return True
    return val.strip().lower() not in _FALSEY


def vtt_path(d: Path) -> Path | None:
    """The existing VTT variant in dir ``d`` (xz preferred), or None."""
    xz = d / XZ
    if xz.exists():
        return xz
    plain = d / PLAIN
    return plain if plain.exists() else None


def read_vtt(d: Path) -> str | None:
    """Decoded VTT text from whichever variant exists in ``d``, else None."""
    p = vtt_path(d)
    if p is None:
        return None
    if p.suffix == ".xz":
        try:
            return lzma.decompress(p.read_bytes()).decode("utf-8")
        except lzma.LZMAError as exc:
            raise lzma.LZMAError(f"corrupt xz transcript: {p}") from exc
    return p.read_text()


def write_vtt(d: Path, text: str, *, compress: bool | None = None) -> Path:
    """Write the canonical VTT for episode dir ``d``.

    ``compress=None`` resolves via :func:`should_compress`. Writes
    ``transcript.vtt.xz`` (lzma) or plain ``transcript.vtt``, removes the other
    variant so a dir never holds both, and returns the path written. Atomic:
    writes a temp file, verifies the compressed bytes round-trip, then
    ``os.replace`` into place; a failure leaves any prior file intact.
    """
    if compress is None:
        compress = should_compress()
    target = d / (XZ if compress else PLAIN)
    other = d / (PLAIN if compress else XZ)
    fd, tmp_name = tempfile.mkstemp(dir=d, prefix=target.name + ".", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        if compress:
            data = lzma.compress(text.encode("utf-8"))
            if lzma.decompress(data).decode("utf-8") != text:
                raise ValueError("xz round-trip verification failed")
            tmp.write_bytes(data)
        else:
            tmp.write_text(text)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    other.unlink(missing_ok=True)
    return target


def compress_dir(d: Path) -> int | None:
    """Rewrite a plain ``transcript.vtt`` as ``.xz``; return bytes reclaimed
    (plain size minus compressed size), or None if nothing to do (already xz,
    or no VTT)."""
    if (d / XZ).exists():
        return None
    plain = d / PLAIN
    if not plain.exists():
        return None
    before = plain.stat().st_size
    write_vtt(d, plain.read_text(), compress=True)
    return before - (d / XZ).stat().st_size


def decompress_dir(d: Path) -> bool:
    """Rewrite a ``transcript.vtt.xz`` as plain ``transcript.vtt``; return True
    if it acted."""
    xz = d / XZ
    if not xz.exists():
        return False
    write_vtt(d, lzma.decompress(xz.read_bytes()).decode("utf-8"), compress=False)
    return True


def _iter_episode_dirs(root: Path) -> Iterator[Path]:
    """Yield ``root/<show>/<episode>/`` directories (the canonical raw layout)."""
    if not root.exists():
        return
    for show in sorted(p for p in root.iterdir() if p.is_dir()):
        for ep in sorted(p for p in show.iterdir() if p.is_dir()):
            yield ep


def migrate_tree(root: Path, *, decompress: bool = False,
                 dry_run: bool = False) -> tuple[int, int]:
    """Walk ``root/<show>/<episode>/`` compressing (or decompressing) VTTs.

    Returns ``(count, bytes_delta)``. For compression, ``bytes_delta`` is bytes
    reclaimed (dry_run computes the real delta by compressing in memory without
    writing); for decompression it is 0.
    """
    count = 0
    delta = 0
    for ep in _iter_episode_dirs(root):
        if decompress:
            if (ep / XZ).exists() and (dry_run or decompress_dir(ep)):
                count += 1
        else:
            plain = ep / PLAIN
            if plain.exists() and not (ep / XZ).exists():
                if dry_run:
                    count += 1
                    compressed = lzma.compress(plain.read_bytes())
                    delta += plain.stat().st_size - len(compressed)
                else:
                    saved = compress_dir(ep)
                    if saved is not None:
                        count += 1
                        delta += saved
    return count, delta

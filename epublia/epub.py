"""Low-level EPUB container handling: read the package, rewrite only changed files."""
from __future__ import annotations

import posixpath
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote

from lxml import etree

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
XHTML_TYPES = {"application/xhtml+xml", "text/html"}
_SAFE_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True, huge_tree=True)


class EpubError(Exception):
    pass


@dataclass
class ManifestItem:
    id: str
    path: str          # full path inside the zip
    media_type: str
    properties: str


class EpubPackage:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.zip = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise EpubError(f"{path.name} is not a valid EPUB/zip file") from exc
        self.names = set(self.zip.namelist())
        if any(n.startswith("META-INF/encryption.xml") for n in self.names):
            enc = self.zip.read("META-INF/encryption.xml")
            if b"EncryptedData" in enc and b"obfuscation" not in enc.lower():
                raise EpubError(f"{path.name} is DRM-protected; it cannot be translated")
        self.opf_path = self._find_opf()
        self.opf_dir = posixpath.dirname(self.opf_path)
        self.opf = etree.fromstring(self.zip.read(self.opf_path), _SAFE_PARSER)
        self.version = self.opf.get("version", "2.0")
        self.manifest: dict[str, ManifestItem] = {}
        for item in self.opf.iterfind(f"{{{OPF_NS}}}manifest/{{{OPF_NS}}}item"):
            href = item.get("href")
            if not href:
                continue
            full = posixpath.normpath(posixpath.join(self.opf_dir, unquote(href)))
            self.manifest[item.get("id")] = ManifestItem(
                item.get("id"), full, item.get("media-type", ""), item.get("properties", "")
            )
        self.spine = [
            ref.get("idref") for ref in self.opf.iterfind(f"{{{OPF_NS}}}spine/{{{OPF_NS}}}itemref")
            if ref.get("idref") in self.manifest
        ]
        spine_el = self.opf.find(f"{{{OPF_NS}}}spine")
        toc_id = spine_el.get("toc") if spine_el is not None else None
        ncx = self.manifest.get(toc_id) if toc_id else None
        if ncx is None:
            ncx = next((m for m in self.manifest.values() if m.media_type == "application/x-dtbncx+xml"), None)
        self.ncx_path = ncx.path if ncx and ncx.path in self.names else None

    def _find_opf(self) -> str:
        try:
            container = etree.fromstring(self.zip.read("META-INF/container.xml"), _SAFE_PARSER)
            rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
            if rootfile is not None and rootfile.get("full-path") in self.names:
                return rootfile.get("full-path")
        except KeyError:
            pass
        opfs = [n for n in self.names if n.lower().endswith(".opf")]
        if not opfs:
            raise EpubError(f"{self.path.name}: no OPF package document found")
        return opfs[0]

    def content_documents(self) -> list[ManifestItem]:
        """XHTML documents in reading order, followed by non-spine ones (e.g. EPUB3 nav)."""
        docs, seen = [], set()
        for idref in self.spine:
            item = self.manifest[idref]
            if item.media_type in XHTML_TYPES and item.path in self.names and item.path not in seen:
                docs.append(item)
                seen.add(item.path)
        for item in self.manifest.values():
            if item.media_type in XHTML_TYPES and item.path in self.names and item.path not in seen:
                docs.append(item)
                seen.add(item.path)
        return docs

    def read(self, name: str) -> bytes:
        return self.zip.read(name)

    @property
    def title(self) -> str:
        el = self.opf.find(f".//{{{DC_NS}}}title")
        return (el.text or "").strip() if el is not None else self.path.stem

    @property
    def author(self) -> str:
        el = self.opf.find(f".//{{{DC_NS}}}creator")
        return (el.text or "").strip() if el is not None else ""

    @property
    def language(self) -> str:
        el = self.opf.find(f".//{{{DC_NS}}}language")
        return (el.text or "").strip() if el is not None else ""

    # ------------------------------------------------------------------ metadata rewrite

    def translated_opf(self, lang: str, lang_name: str, model: str) -> tuple[bytes, str | None, str | None]:
        """Return (opf_bytes, old_uid, new_uid). Sets dc:language, gives the translation its own
        identifier (so reading apps don't merge it with the original) and records the translator."""
        opf = etree.fromstring(etree.tostring(self.opf), _SAFE_PARSER)
        metadata = opf.find(f"{{{OPF_NS}}}metadata")
        if metadata is None:
            return etree.tostring(opf, xml_declaration=True, encoding="utf-8"), None, None

        langs = metadata.findall(f"{{{DC_NS}}}language")
        if langs:
            langs[0].text = lang
            for extra in langs[1:]:
                metadata.remove(extra)
        else:
            etree.SubElement(metadata, f"{{{DC_NS}}}language").text = lang

        old_uid = new_uid = None
        uid_attr = opf.get("unique-identifier")
        ident = None
        if uid_attr:
            ident = next((e for e in metadata.iter(f"{{{DC_NS}}}identifier") if e.get("id") == uid_attr), None)
        if ident is not None and (ident.text or "").strip():
            old_uid = ident.text.strip()
            new_uid = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, f"epublia:{old_uid}:{lang}"))
            if not old_uid.startswith("urn:uuid:"):
                new_uid = new_uid[len("urn:uuid:"):]
            ident.text = new_uid

        contributor = etree.SubElement(metadata, f"{{{DC_NS}}}contributor")
        contributor.text = f"epublia ({model}) — {lang_name} translation"
        if self.version.startswith("2"):
            contributor.set(f"{{{OPF_NS}}}role", "trl")
        return etree.tostring(opf, xml_declaration=True, encoding="utf-8"), old_uid, new_uid


def write_epub(src: zipfile.ZipFile, dest: Path, replacements: dict[str, bytes]) -> None:
    """Copy `src` to `dest`, swapping in `replacements`. `mimetype` goes first and uncompressed
    as the OCF spec requires; everything else keeps its original order."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with zipfile.ZipFile(tmp, "w") as out:
        out.writestr(zipfile.ZipInfo("mimetype"), b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for info in src.infolist():
            if info.filename == "mimetype":
                continue
            data = replacements.get(info.filename)
            if data is None:
                data = src.read(info.filename)
            new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
            new.external_attr = info.external_attr
            compress = zipfile.ZIP_STORED if info.filename.endswith("/") else zipfile.ZIP_DEFLATED
            out.writestr(new, data, compress_type=compress)
    tmp.replace(dest)

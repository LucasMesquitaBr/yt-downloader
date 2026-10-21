"""
YouTube Downloader - Backend Flask + yt-dlp
Metadados: yt-dlp + iTunes API + MusicBrainz API
"""

import json as _json
import shutil
import ssl
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

import os

from flask import Flask, jsonify, render_template_string, request, send_file
import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "yt_downloader"
DOWNLOAD_DIR.mkdir(exist_ok=True)

COOKIE_PATH = Path(__file__).parent / "cookies.txt"

# FFmpeg: detecta se está no Linux (nuvem) ou no caminho do Windows local
WIN_FFMPEG = r"C:\Users\Lucas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0.2-full_build\bin"
if os.path.exists(WIN_FFMPEG):
    FFMPEG_PATH = WIN_FFMPEG
else:
    FFMPEG_PATH = shutil.which("ffmpeg") or "ffmpeg"

HTML = open(Path(__file__).parent / "index.html", encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════
#  HTTP helper
# ══════════════════════════════════════════════════════════════════

def _http_get(url: str, timeout: int = 12, extra_headers: dict = None) -> bytes:
    headers = {"User-Agent": "YTDownloader/2.0 (github.com/local)"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()

def _json_get(url: str, timeout: int = 12, extra_headers: dict = None) -> dict:
    return _json.loads(_http_get(url, timeout=timeout, extra_headers=extra_headers))


import re

def _clean_track_title(title: str) -> str:
    """Limpa sufixos de vídeo como (Official Video), (2011 Remaster), feat, etc."""
    if not title:
        return ""
    t = title
    t = re.sub(r'[\(\[\{].*?(official|video|audio|clip|remaster|hd|4k|lyric|live|remix).*?[\)\]\}]', '', t, flags=re.IGNORECASE)
    t = re.sub(r'[\(\[\{]\d{4}\s*remaster.*?[\)\]\}]', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s*-\s*(official|video|audio|remaster).*?$', '', t, flags=re.IGNORECASE)
    t = re.sub(r'\s+', ' ', t).strip(' -')
    return t or title

# ══════════════════════════════════════════════════════════════════
#  FONTE 1 — iTunes Search API
# ══════════════════════════════════════════════════════════════════

def _fetch_itunes(artist: str, track: str, album: str) -> dict:
    """Retorna: cover_data (bytes), e campos básicos de metadados."""
    result = {}
    clean_track = _clean_track_title(track)

    def _search(term: str, entity: str = "song", limit: int = 5) -> list:
        url = (
            "https://itunes.apple.com/search?"
            + urllib.parse.urlencode({"term": term, "entity": entity, "limit": limit})
        )
        try:
            data = _json_get(url)
            return data.get("results", [])
        except Exception:
            return []

    def _artwork(r: dict) -> str:
        u = r.get("artworkUrl100", "")
        return u.replace("100x100bb", "3000x3000bb").replace("100x100", "3000x3000bb")

    # Tentativas de busca
    items = []
    if clean_track and artist:
        items = _search(f"{clean_track} {artist}", "song")
    if not items and track and artist:
        items = _search(f"{track} {artist}", "song")
    if not items and album and artist:
        items = _search(f"{album} {artist}", "album")
    if not items and clean_track:
        items = _search(clean_track, "song")
    if not items and track:
        items = _search(track, "song")

    if not items:
        return result

    r = items[0]
    result["itunes_track"]        = r.get("trackName", "")
    result["itunes_artist"]       = r.get("artistName", "")
    result["itunes_album"]        = r.get("collectionName", "")
    result["itunes_album_artist"] = r.get("collectionArtistName", "") or r.get("artistName", "")
    result["itunes_genre"]        = r.get("primaryGenreName", "")
    result["itunes_year"]         = str(r.get("releaseDate", ""))[:4]
    result["itunes_track_num"]    = r.get("trackNumber")
    result["itunes_track_total"]  = r.get("trackCount")
    result["itunes_disc_num"]     = r.get("discNumber")
    result["itunes_disc_total"]   = r.get("discCount")
    result["itunes_country"]      = r.get("country", "")
    result["itunes_explicit"]     = r.get("trackExplicitness", "")
    result["itunes_composer"]     = r.get("composerName", "")
    result["itunes_duration_ms"]  = r.get("trackTimeMillis")

    # Capa em alta resolução
    art_url = _artwork(r)
    if art_url:
        try:
            result["cover_data"] = _http_get(art_url, timeout=20)
            print(f"[iTunes] Capa baixada: {art_url}")
        except Exception as e:
            print(f"[iTunes] Erro ao baixar capa: {e}")

    return result


# ══════════════════════════════════════════════════════════════════
#  FONTE 2 — MusicBrainz API
# ══════════════════════════════════════════════════════════════════

MB_HEADERS = {
    "User-Agent": "YTDownloader/2.0 (local-app)",
    "Accept":     "application/json",
}
MB_BASE = "https://musicbrainz.org/ws/2"


def _mb_get(path: str, params: dict = None) -> dict:
    qs = urllib.parse.urlencode({"fmt": "json", **(params or {})})
    url = f"{MB_BASE}/{path}?{qs}"
    time.sleep(1.1)   # MusicBrainz: máx 1 req/s
    return _json_get(url, timeout=20, extra_headers=MB_HEADERS)


def _fetch_musicbrainz(artist: str, track: str, album: str) -> dict:
    """
    Retorna metadados detalhados do MusicBrainz:
    ISRC, gravadora, catálogo, estúdio, produtor, engenheiro,
    compositor, letrista, arranjos, país, código de barras, etc.
    """
    result = {}

    try:
        clean_track = _clean_track_title(track)

        # ── 1. Busca a gravação (recording) ──────────────────────
        def _do_search(t, a, alb):
            p = []
            if t: p.append(f'recording:"{t}"')
            if a: p.append(f'artistname:"{a}"')
            if alb: p.append(f'release:"{alb}"')
            s = _mb_get("recording", {"query": " AND ".join(p), "limit": 5})
            return s.get("recordings", [])

        recordings = []
        if clean_track:
            recordings = _do_search(clean_track, artist, album)
        if not recordings and track:
            recordings = _do_search(track, artist, album)
        if not recordings and clean_track and artist:
            recordings = _do_search(clean_track, artist, None)
        if not recordings and clean_track:
            recordings = _do_search(clean_track, None, None)

        if not recordings:
            print("[MusicBrainz] Nenhuma gravação encontrada.")
            return result

        rec_id = recordings[0].get("id")
        if not rec_id:
            return result

        # ── 2. Detalhes da gravação com relações ─────────────────
        rec = _mb_get(
            f"recording/{rec_id}",
            {
                "inc": (
                    "artists+releases+isrcs"
                    "+artist-rels+work-rels+work-level-rels"
                    "+recording-rels+tags+ratings"
                )
            },
        )

        result["mb_recording_id"] = rec.get("id", "")
        result["mb_title"]        = rec.get("title", "")
        result["mb_length_ms"]    = rec.get("length")

        # ISRCs
        isrcs = rec.get("isrcs", [])
        result["isrc"] = isrcs[0] if isrcs else ""

        # Rating
        rating = rec.get("rating", {})
        if rating.get("value"):
            result["mb_rating"] = str(round(float(rating["value"]), 1))

        # Tags/géneros
        tags = sorted(rec.get("tags", []), key=lambda t: t.get("count", 0), reverse=True)
        if tags:
            result["mb_genre"] = tags[0]["name"]
            result["mb_tags"]  = "; ".join(t["name"] for t in tags[:5])

        # ── 3. Relações da gravação (produtor, engenheiro, estúdio…) ──
        for rel in rec.get("relations", []):
            rel_type  = rel.get("type", "").lower()
            direction = rel.get("direction", "")
            entity    = rel.get("artist") or rel.get("place") or rel.get("work") or {}
            name      = entity.get("name", "")

            if not name:
                continue

            if rel_type in ("producer", "producer"):
                result.setdefault("producers", []).append(name)
            elif rel_type in ("engineer", "audio"):
                result.setdefault("engineers", []).append(name)
            elif rel_type == "mix":
                result.setdefault("mixers", []).append(name)
            elif rel_type in ("mastering", "mastered by"):
                result.setdefault("mastering_engineers", []).append(name)
            elif rel_type in ("recorded at", "recording"):
                result.setdefault("studios", []).append(name)
            elif rel_type in ("vocal", "vocals", "lead vocals", "background vocals"):
                result.setdefault("vocalists", []).append(name)
            elif rel_type in ("composer", "music by"):
                result.setdefault("composers", []).append(name)
            elif rel_type in ("lyricist", "lyrics by"):
                result.setdefault("lyricists", []).append(name)
            elif rel_type in ("arranger", "arranged by"):
                result.setdefault("arrangers", []).append(name)
            elif rel_type in ("instrument", "instrumentalist", "guitar", "bass", "drums",
                              "keyboards", "piano", "violin", "cello", "trumpet", "saxophone"):
                result.setdefault("performers", []).append(f"{name} ({rel_type})")
            elif rel_type in ("performer", "performing orchestra"):
                result.setdefault("performers", []).append(name)

        # ── 4. Lançamento (label, catálogo, país…) ────────────────
        releases = rec.get("releases", [])
        if not releases:
            return result

        official = [r for r in releases if r.get("status") == "Official"]
        release  = official[0] if official else releases[0]
        rel_id   = release.get("id")

        result["mb_album"]          = release.get("title", "")
        result["mb_release_status"] = release.get("status", "")
        result["mb_country"]        = release.get("country", "")
        result["mb_release_date"]   = release.get("date", "")

        # Track number from this release
        for medium in release.get("media", []):
            for trk in medium.get("tracks", []):
                if trk.get("recording", {}).get("id") == rec_id:
                    result["mb_track_number"] = trk.get("number", "")
                    result["mb_disc_number"]  = str(medium.get("position", ""))
                    result["mb_total_tracks"] = str(medium.get("track-count", ""))
                    result["mb_disc_format"]  = medium.get("format", "")

        if not rel_id:
            return result

        # ── 5. Release completo (gravadora, código de barras, anotação…) ──
        full = _mb_get(
            f"release/{rel_id}",
            {
                "inc": (
                    "artists+labels+recordings+release-groups"
                    "+artist-credits+annotation+artist-rels"
                    "+recording-level-rels+work-level-rels"
                )
            },
        )

        # Gravadora / label
        label_info = full.get("label-info", [])
        labels, catalogs = [], []
        for li in label_info:
            lbl = li.get("label") or {}
            if lbl.get("name"):
                labels.append(lbl["name"])
            if li.get("catalog-number"):
                catalogs.append(li["catalog-number"])
        if labels:
            result["label"]          = "; ".join(labels)
        if catalogs:
            result["catalog_number"] = "; ".join(catalogs)

        # Grupo de lançamento
        rg = full.get("release-group", {})
        result["mb_release_group_id"] = rg.get("id", "")
        result["mb_album_type"]       = rg.get("primary-type", "")
        secondary                     = rg.get("secondary-types", [])
        if secondary:
            result["mb_album_secondary_type"] = "; ".join(secondary)

        # Código de barras, embalagem, idioma, script
        result["barcode"]    = full.get("barcode", "")
        result["packaging"]  = full.get("packaging", "")
        result["mb_language"]= full.get("text-representation", {}).get("language", "")
        result["mb_script"]  = full.get("text-representation", {}).get("script", "")

        # Anotação (notas do release — pode conter info de estúdio)
        annotation = full.get("annotation", "")
        if annotation:
            result["annotation"] = annotation[:500]  # limitar tamanho

        # Artista do álbum (artist-credit)
        ac_list = full.get("artist-credit", [])
        album_artists = [
            (ac.get("name") or ac.get("artist", {}).get("name", ""))
            for ac in ac_list
            if isinstance(ac, dict)
        ]
        result["mb_album_artist"] = "; ".join(filter(None, album_artists))

        # Relações a nível de lançamento (estúdio, mastering, etc.)
        for rel in full.get("relations", []):
            rel_type = rel.get("type", "").lower()
            entity   = rel.get("place") or rel.get("artist") or {}
            name     = entity.get("name", "")
            if not name:
                continue
            if rel_type in ("recorded at", "recording", "mixed at", "mastered at"):
                result.setdefault("studios", []).append(f"{name} ({rel_type})")
            elif rel_type == "producer":
                result.setdefault("producers", []).append(name)
            elif rel_type in ("engineer", "audio engineer"):
                result.setdefault("engineers", []).append(name)

    except Exception as e:
        print(f"[MusicBrainz] Erro: {e}")

    return result


# ══════════════════════════════════════════════════════════════════
#  Embutir TODOS os metadados no arquivo
# ══════════════════════════════════════════════════════════════════

def embed_all_metadata(filepath: Path, ytdlp_info: dict, itunes: dict, mb: dict) -> None:
    """
    Combina metadados de yt-dlp, iTunes e MusicBrainz e embute no arquivo.
    Prioridade: MusicBrainz > iTunes > yt-dlp para campos duplicados.
    """
    suffix = filepath.suffix.lower()

    # ── helpers de merge (prioridade: mb > itunes > yt-dlp) ──────
    def pick(*values):
        return next((v for v in values if v), "")

    def join_list(key: str) -> str:
        items = mb.get(key, [])
        return "; ".join(dict.fromkeys(items)) if items else ""  # deduplica, preserva ordem

    artist       = pick(mb.get("mb_album_artist"), itunes.get("itunes_artist"),
                        ytdlp_info.get("artist"), ytdlp_info.get("uploader"))
    title        = pick(mb.get("mb_title"), itunes.get("itunes_track"), ytdlp_info.get("title"))
    album        = pick(mb.get("mb_album"), itunes.get("itunes_album"), ytdlp_info.get("album"))
    album_artist = pick(mb.get("mb_album_artist"), itunes.get("itunes_album_artist"), artist)
    year         = pick(mb.get("mb_release_date", "")[:4], itunes.get("itunes_year"),
                        str(ytdlp_info.get("release_year", "")),
                        (ytdlp_info.get("release_date") or "")[:4])
    genre        = pick(mb.get("mb_genre"), itunes.get("itunes_genre"), ytdlp_info.get("genre"))
    composer     = pick(join_list("composers"), itunes.get("itunes_composer"))
    lyricist     = join_list("lyricists")
    arranger     = join_list("arrangers")
    producer     = join_list("producers")
    engineer     = join_list("engineers")
    mixer        = join_list("mixers")
    mastering    = join_list("mastering_engineers")
    studio       = join_list("studios")
    performers   = join_list("performers")
    label        = mb.get("label", "")
    catalog      = mb.get("catalog_number", "")
    isrc         = mb.get("isrc", "")
    country      = pick(mb.get("mb_country"), itunes.get("itunes_country"))
    language     = mb.get("mb_language", "")
    script       = mb.get("mb_script", "")
    barcode      = mb.get("barcode", "")
    packaging    = mb.get("packaging", "")
    album_type   = mb.get("mb_album_type", "")
    mb_tags      = mb.get("mb_tags", "")
    annotation   = mb.get("annotation", "")
    mb_rec_id    = mb.get("mb_recording_id", "")
    mb_rg_id     = mb.get("mb_release_group_id", "")
    rating       = mb.get("mb_rating", "")

    trk_num  = pick(mb.get("mb_track_number"), str(itunes.get("itunes_track_num") or ""))
    trk_tot  = pick(mb.get("mb_total_tracks"), str(itunes.get("itunes_track_total") or ""))
    disc_num = pick(mb.get("mb_disc_number"), str(itunes.get("itunes_disc_num") or ""))
    disc_tot = pick(mb.get("mb_disc_total"),  str(itunes.get("itunes_disc_total") or ""))

    cover_data = itunes.get("cover_data")

    if suffix == ".mp3":
        _embed_mp3(filepath, locals())
    elif suffix in (".mp4", ".m4a", ".m4v"):
        _embed_mp4(filepath, locals())
    else:
        print(f"[Embed] Formato {suffix} não suportado para embed de metadados.")


def _embed_mp3(filepath: Path, ctx: dict) -> None:
    from mutagen.id3 import (
        ID3, APIC, COMM, TALB, TBPM, TCOM, TCON, TCOP, TDRC,
        TEXT, TIT2, TLAN, TMED, TOAL, TPE1, TPE2, TPOS, TPUB,
        TRCK, TSRC, TXXX, error as ID3Error,
    )
    from mutagen.mp3 import MP3

    audio = MP3(str(filepath), ID3=ID3)
    try:
        audio.add_tags()
    except ID3Error:
        pass
    tags = audio.tags

    def tx(desc, val):
        if val:
            tags.add(TXXX(encoding=3, desc=desc, text=val))

    def set_tag(cls, *args, **kwargs):
        try:
            tags.add(cls(*args, **kwargs))
        except Exception:
            pass

    # ── Tags padrão ──────────────────────────────────────────────
    if ctx["title"]:       set_tag(TIT2, encoding=3, text=ctx["title"])
    if ctx["artist"]:      set_tag(TPE1, encoding=3, text=ctx["artist"])
    if ctx["album_artist"]:set_tag(TPE2, encoding=3, text=ctx["album_artist"])
    if ctx["album"]:       set_tag(TALB, encoding=3, text=ctx["album"])
    if ctx["year"]:        set_tag(TDRC, encoding=3, text=ctx["year"])
    if ctx["genre"]:       set_tag(TCON, encoding=3, text=ctx["genre"])
    if ctx["composer"]:    set_tag(TCOM, encoding=3, text=ctx["composer"])
    if ctx["lyricist"]:    set_tag(TEXT, encoding=3, text=ctx["lyricist"])
    if ctx["label"]:       set_tag(TPUB, encoding=3, text=ctx["label"])
    if ctx["isrc"]:        set_tag(TSRC, encoding=3, text=ctx["isrc"])
    if ctx["language"]:    set_tag(TLAN, encoding=3, text=ctx["language"])
    if ctx["album_type"]:  set_tag(TMED, encoding=3, text=ctx["album_type"])

    # Faixa e disco
    trk = ctx["trk_num"] + ("/" + ctx["trk_tot"] if ctx["trk_tot"] else "")
    dsk = ctx["disc_num"] + ("/" + ctx["disc_tot"] if ctx["disc_tot"] else "")
    if trk: set_tag(TRCK, encoding=3, text=trk)
    if dsk: set_tag(TPOS, encoding=3, text=dsk)

    # Copyright
    if ctx["label"] and ctx["year"]:
        set_tag(TCOP, encoding=3, text=f"℗ {ctx['year']} {ctx['label']}")

    # Comentário com informações extras
    comment_lines = []
    if ctx["producer"]:    comment_lines.append(f"Produzido por: {ctx['producer']}")
    if ctx["engineer"]:    comment_lines.append(f"Engenheiro: {ctx['engineer']}")
    if ctx["mixer"]:       comment_lines.append(f"Mixagem: {ctx['mixer']}")
    if ctx["mastering"]:   comment_lines.append(f"Masterização: {ctx['mastering']}")
    if ctx["studio"]:      comment_lines.append(f"Estúdio: {ctx['studio']}")
    if ctx["arranger"]:    comment_lines.append(f"Arranjos: {ctx['arranger']}")
    if ctx["performers"]:  comment_lines.append(f"Performers: {ctx['performers']}")
    if ctx["catalog"]:     comment_lines.append(f"Catálogo: {ctx['catalog']}")
    if ctx["barcode"]:     comment_lines.append(f"Código de barras: {ctx['barcode']}")
    if ctx["country"]:     comment_lines.append(f"País: {ctx['country']}")
    if ctx["annotation"]:  comment_lines.append(f"Notas: {ctx['annotation']}")
    if comment_lines:
        set_tag(COMM, encoding=3, lang="por", desc="Info", text="\n".join(comment_lines))

    # ── Tags customizadas TXXX ────────────────────────────────────
    tx("PRODUCER",            ctx["producer"])
    tx("ENGINEER",            ctx["engineer"])
    tx("MIXER",               ctx["mixer"])
    tx("MASTERING_ENGINEER",  ctx["mastering"])
    tx("STUDIO",              ctx["studio"])
    tx("LYRICIST",            ctx["lyricist"])
    tx("ARRANGER",            ctx["arranger"])
    tx("PERFORMERS",          ctx["performers"])
    tx("LABEL",               ctx["label"])
    tx("CATALOGNUMBER",       ctx["catalog"])
    tx("BARCODE",             ctx["barcode"])
    tx("COUNTRY",             ctx["country"])
    tx("SCRIPT",              ctx["script"])
    tx("PACKAGING",           ctx["packaging"])
    tx("ALBUMTYPE",           ctx["album_type"])
    tx("TAGS",                ctx["mb_tags"])
    tx("RATING",              ctx["rating"])
    tx("ISRC",                ctx["isrc"])
    tx("MusicBrainz Recording Id", ctx["mb_rec_id"])
    tx("MusicBrainz Release Group Id", ctx["mb_rg_id"])

    # ── Capa oficial ──────────────────────────────────────────────
    if ctx["cover_data"]:
        tags.delall("APIC")
        tags.add(APIC(
            encoding=3,
            mime="image/jpeg",
            type=3,
            desc="Cover",
            data=ctx["cover_data"],
        ))

    audio.save(v2_version=3)
    print(f"[Embed] MP3 — {len([k for k,v in ctx.items() if v and k not in ('cover_data','filepath','ctx','suffix','tags','audio')])} campos embutidos.")


def _embed_mp4(filepath: Path, ctx: dict) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
    from mutagen.mp4 import AtomDataType

    video = MP4(str(filepath))
    if video.tags is None:
        video.add_tags()
    t = video.tags

    def st(key, val):
        if val:
            t[key] = [val]

    def ff(key, val):
        """FreeForm tag para campos extras."""
        if val:
            t[f"----:com.apple.iTunes:{key}"] = [
                MP4FreeForm(val.encode("utf-8"), dataformat=AtomDataType.UTF8)
            ]

    st("©nam", ctx["title"])
    st("©ART", ctx["artist"])
    st("aART", ctx["album_artist"])
    st("©alb", ctx["album"])
    st("©day", ctx["year"])
    st("©gen", ctx["genre"])
    st("©wrt", ctx["composer"])
    st("©lyr", ctx["lyricist"])   # Não padrão mas aceito por vários players
    st("cprt", f"℗ {ctx['year']} {ctx['label']}" if ctx["label"] and ctx["year"] else "")

    # Track/disc como tuplas
    try:
        if ctx["trk_num"]:
            num = int(ctx["trk_num"])
            tot = int(ctx["trk_tot"]) if ctx["trk_tot"] else 0
            t["trkn"] = [(num, tot)]
    except Exception:
        pass
    try:
        if ctx["disc_num"]:
            num = int(ctx["disc_num"])
            tot = int(ctx["disc_tot"]) if ctx["disc_tot"] else 0
            t["disk"] = [(num, tot)]
    except Exception:
        pass

    # Comentário
    comment_lines = []
    if ctx["producer"]:   comment_lines.append(f"Producer: {ctx['producer']}")
    if ctx["engineer"]:   comment_lines.append(f"Engineer: {ctx['engineer']}")
    if ctx["mixer"]:      comment_lines.append(f"Mixer: {ctx['mixer']}")
    if ctx["mastering"]:  comment_lines.append(f"Mastering: {ctx['mastering']}")
    if ctx["studio"]:     comment_lines.append(f"Studio: {ctx['studio']}")
    if comment_lines:
        st("©cmt", "\n".join(comment_lines))

    # Tags FreeForm (iTunes / players avançados)
    ff("PRODUCER",             ctx["producer"])
    ff("ENGINEER",             ctx["engineer"])
    ff("MIXER",                ctx["mixer"])
    ff("MASTERING_ENGINEER",   ctx["mastering"])
    ff("STUDIO",               ctx["studio"])
    ff("LYRICIST",             ctx["lyricist"])
    ff("ARRANGER",             ctx["arranger"])
    ff("PERFORMERS",           ctx["performers"])
    ff("LABEL",                ctx["label"])
    ff("CATALOGNUMBER",        ctx["catalog"])
    ff("ISRC",                 ctx["isrc"])
    ff("BARCODE",              ctx["barcode"])
    ff("COUNTRY",              ctx["country"])
    ff("LANGUAGE",             ctx["language"])
    ff("SCRIPT",               ctx["script"])
    ff("PACKAGING",            ctx["packaging"])
    ff("ALBUMTYPE",            ctx["album_type"])
    ff("TAGS",                 ctx["mb_tags"])
    ff("RATING",               ctx["rating"])
    ff("MusicBrainz Recording Id",      ctx["mb_rec_id"])
    ff("MusicBrainz Release Group Id",  ctx["mb_rg_id"])

    # Capa oficial
    if ctx["cover_data"]:
        t["covr"] = [MP4Cover(ctx["cover_data"], imageformat=MP4Cover.FORMAT_JPEG)]

    video.save()
    print(f"[Embed] MP4 — metadados embutidos com sucesso.")


def _download_single_item(url: str, target_dir: Path, format_id: str, audio_only: bool, mp3_quality: str) -> Path:
    """Baixa um item único para target_dir e embute todos os metadados (iTunes + MusicBrainz)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(target_dir / "%(title)s.%(ext)s")
    common_pp = [{"key": "FFmpegMetadata", "add_metadata": True}]

    common_extractor_args = {
        "youtube": {
            "player_client": ["visionos", "android"]
        }
    }

    common_ydl_params = {
        "quiet": True,
        "ffmpeg_location": FFMPEG_PATH,
        "js_runtimes": {"node": {}},
        "remote_components": ["ejs:github"],
    }
    if COOKIE_PATH.exists():
        common_ydl_params["cookiefile"] = str(COOKIE_PATH)
    else:
        common_ydl_params["extractor_args"] = common_extractor_args

    if audio_only:
        ydl_opts = {
            **common_ydl_params,
            "format": "bestaudio/ba/b/best",
            "outtmpl": output_template,
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": mp3_quality},
                *common_pp,
            ],
        }
    else:
        ydl_opts = {
            **common_ydl_params,
            "format": f"{format_id}+bestaudio/best",
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "postprocessors": common_pp,
        }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ytdlp_info = ydl.extract_info(url, download=True)

    video_exts = {".mp4", ".mkv", ".webm", ".mp3", ".m4a", ".ogg", ".opus"}
    candidates = [f for f in target_dir.glob("*") if f.suffix.lower() in video_exts]
    if not candidates:
        candidates = [f for f in target_dir.glob("*") if f.suffix.lower() not in {".jpg", ".jpeg", ".webp", ".png"}]
    if not candidates:
        raise RuntimeError("Arquivo não encontrado após download.")

    filepath = candidates[0]

    artist = ytdlp_info.get("artist") or ytdlp_info.get("uploader") or ""
    track  = ytdlp_info.get("track")  or ytdlp_info.get("title")    or ""
    album  = ytdlp_info.get("album")  or ""

    print(f"\n{'='*55}")
    print(f"[Meta] Buscando metadados para: {track} - {artist}")
    print(f"{'='*55}")

    itunes = _fetch_itunes(artist, track, album)
    mb     = _fetch_musicbrainz(artist, track, album)
    embed_all_metadata(filepath, ytdlp_info, itunes, mb)

    return filepath


# ══════════════════════════════════════════════════════════════════
#  Rotas Flask
# ══════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return send_file(Path(__file__).parent / "index.html")


@app.route("/manifest.json")
def manifest():
    return send_file(Path(__file__).parent / "manifest.json", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    return send_file(Path(__file__).parent / "sw.js", mimetype="application/javascript")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_file(Path(__file__).parent / "static" / filename)


@app.route("/app.apk")
def download_apk():
    apk_file = Path(__file__).parent / "YouTubeDownloader.apk"
    if apk_file.exists():
        return send_file(apk_file, as_attachment=True, download_name="YouTubeDownloader.apk", mimetype="application/vnd.android.package-archive")
    return jsonify({"error": "APK não encontrado"}), 404


TOKEN_FILE = Path(__file__).parent / "spotify_token_cache.json"

def get_spotify_tokens():
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE, encoding="utf-8") as f:
                data = _json.load(f)
                if data.get("expires_at", 0) > time.time() + 300:
                    return data
        except Exception as e:
            print("[Spotify Cache Error]:", e)

    print("[Spotify] Obtendo novos tokens do Web Player...")
    tokens = {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            
            def handle_response(response):
                if "api/token" in response.url:
                    try:
                        res_json = response.json()
                        if isinstance(res_json, dict) and "accessToken" in res_json:
                            tokens["access_token"] = res_json["accessToken"]
                            exp_ms = res_json.get("accessTokenExpirationTimestampMs", 0)
                            tokens["expires_at"] = (exp_ms / 1000) if exp_ms else (time.time() + 3600)
                    except:
                        pass
                elif "clienttoken.spotify.com" in response.url:
                    try:
                        res_json = response.json()
                        tokens["client_token"] = res_json.get("granted_token", {}).get("token")
                    except:
                        pass
                        
            page.on("response", handle_response)
            page.goto("https://open.spotify.com", wait_until="networkidle", timeout=20000)
            browser.close()

        if tokens.get("access_token") and tokens.get("client_token"):
            with open(TOKEN_FILE, "w", encoding="utf-8") as f:
                _json.dump(tokens, f, indent=2)
            print("[Spotify] Novos tokens salvos com sucesso!")
            return tokens
    except Exception as e:
        print("[Spotify Token Error]:", e)
    return tokens


def search_spotify(query, limit=10):
    tokens = get_spotify_tokens()
    if not tokens or not tokens.get("access_token"):
        return []

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": "https://open.spotify.com",
        "Referer": "https://open.spotify.com/",
        "App-Platform": "WebPlayer",
        "Authorization": f"Bearer {tokens['access_token']}",
        "Client-Token": tokens.get("client_token", ""),
        "spotify-app-version": "1.3.7.1581.gc64768c11c44",
        "sec-ch-ua-platform": "\"Windows\"",
    }

    payload = {
        "variables": {
            "searchTerm": query,
            "offset": 0,
            "limit": limit,
            "numberOfTopResults": 5,
            "includeAudiobooks": False,
            "includeArtistHasConcertsField": False,
            "includePreReleases": False,
            "includeAlbumPreReleases": False,
            "includeAuthors": False,
            "includeEpisodeContentRatingsV2": False,
            "isPrefix": None,
            "sectionFilters": ["GENERIC"]
        },
        "operationName": "searchDesktop",
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": "eef7cc54888d91bdd6802623477873caa3948ae173a0c34fd86827b267e94c03"
            }
        }
    }

    ctx = ssl._create_unverified_context()
    req = urllib.request.Request(
        "https://api-partner.spotify.com/pathfinder/v2/query",
        data=_json.dumps(payload).encode("utf-8"),
        headers=headers
    )

    with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
        data = _json.loads(resp.read().decode("utf-8"))
        items = data.get("data", {}).get("searchV2", {}).get("tracksV2", {}).get("items", [])
        
        results = []
        for t in items:
            track = t.get("item", {}).get("data", {})
            if not track:
                continue
            name = track.get("name")
            artists = [a.get("profile", {}).get("name") for a in track.get("artists", {}).get("items", []) if a.get("profile", {}).get("name")]
            artist_str = ", ".join(artists) if artists else "Artista Desconhecido"
            album = track.get("albumOfTrack", {}).get("name") or ""
            dur_ms = track.get("duration", {}).get("totalMilliseconds") or 0
            
            cover_sources = track.get("albumOfTrack", {}).get("coverArt", {}).get("sources", [])
            cover_url = cover_sources[0].get("url") if cover_sources else ""
            
            uri = track.get("uri") or ""
            track_id = uri.split(":")[-1] if uri else ""
            spotify_url = f"https://open.spotify.com/track/{track_id}" if track_id else ""
            
            results.append({
                "id": track_id,
                "title": name,
                "artist": artist_str,
                "uploader": artist_str,
                "album": album,
                "duration": dur_ms // 1000,
                "thumbnail": cover_url,
                "url": spotify_url,
                "source": "Spotify"
            })
            
        return results


def resolve_spotify_to_youtube(title, artist):
    clean_t = _clean_track_title(title)
    clean_a = artist.split(',')[0].split(';')[0].strip() if artist else ''
    queries = []
    if clean_t and clean_a:
        queries.append(f"{clean_a} - {clean_t} audio")
    if clean_t and artist and artist != clean_a:
        queries.append(f"{artist} - {clean_t} audio")
    if title and artist:
        queries.append(f"{artist} - {title} audio")
    if clean_t:
        queries.append(f"{clean_t} audio")
    if title and title != clean_t:
        queries.append(f"{title} audio")
    if clean_t and clean_a:
        queries.append(f"{clean_a} - {clean_t}")
    if clean_t:
        queries.append(clean_t)

    ydl_opts = {
        "quiet": True,
        "extract_flat": True,
        "skip_download": True,
        "extractor_args": {"youtube": {"player_client": ["visionos", "android"]}},
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        for q in queries:
            try:
                res = ydl.extract_info(f"ytsearch1:{q}", download=False)
                if res and res.get("entries"):
                    e = res["entries"][0]
                    v_id = e.get("id")
                    if v_id:
                        return f"https://www.youtube.com/watch?v={v_id}", e.get("title")
            except Exception:
                pass
    return None, None


def _apply_spotify_metadata_to_mp3(filepath, title, artist, album, cover_url):
    try:
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
        from mutagen.mp3 import MP3

        audio = MP3(filepath)
        if audio.tags is None:
            audio.add_tags()
        
        if title:
            audio.tags["TIT2"] = TIT2(encoding=3, text=title)
        if artist:
            audio.tags["TPE1"] = TPE1(encoding=3, text=artist)
        if album:
            audio.tags["TALB"] = TALB(encoding=3, text=album)
        
        if cover_url:
            try:
                ctx = ssl._create_unverified_context()
                req = urllib.request.Request(cover_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10, context=ctx) as r:
                    img_data = r.read()
                    audio.tags["APIC"] = APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=img_data
                    )
            except Exception as e_cover:
                print(f"[Spotify Cover Error]: {e_cover}")

        audio.save()
        print(f"[Spotify] Tags aplicadas com sucesso em: {filepath.name}")
    except Exception as e:
        print(f"[Spotify Metadata Error]: {e}")


@app.route("/api/search", methods=["POST"])
def search_tracks():
    """Pesquisa músicas no Spotify pelo nome (com fallback para YouTube Music)."""
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Digite o nome da música ou artista"}), 400

    limit = int(data.get("limit", 12))
    limit = max(1, min(limit, 25))

    # 1. Busca direta prioritária no Spotify
    try:
        spotify_results = search_spotify(query, limit=limit)
        if spotify_results:
            return jsonify({"success": True, "results": spotify_results, "source": "Spotify"})
    except Exception as e_spot:
        print(f"[Search Spotify Error]: {e_spot}")

    # 2. Fallback YouTube Music caso o Spotify falhe
    try:
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
        }
        music_url = f"https://music.youtube.com/search?q={urllib.parse.quote(query)}"
        raw_entries = []
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(music_url, download=False)
                if res and res.get("entries"):
                    raw_entries = [
                        e for e in res.get("entries", [])
                        if e and e.get("title") and ("watch?v=" in (e.get("url") or "") or e.get("id"))
                    ]
        except Exception as e_music:
            print(f"[Search] Aviso na busca YouTube Music: {e_music}")

        if not raw_entries:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                res = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                raw_entries = res.get("entries", []) if res else []

        results = []
        for e in raw_entries[:limit]:
            if not e:
                continue
            vid_id = e.get("id") or ""
            title = e.get("title") or "Sem título"
            uploader = e.get("uploader") or e.get("channel") or "YouTube Music"
            duration = e.get("duration") or 0

            thumb_url = ""
            thumbnails = e.get("thumbnails") or []
            if thumbnails:
                thumb_url = thumbnails[-1].get("url") or ""
            if not thumb_url and vid_id:
                thumb_url = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"

            video_url = f"https://music.youtube.com/watch?v={vid_id}" if vid_id else (e.get("url") or "")

            results.append({
                "id": vid_id,
                "title": title,
                "uploader": uploader,
                "duration": duration,
                "thumbnail": thumb_url,
                "url": video_url,
                "source": "YouTube Music"
            })

        return jsonify({"success": True, "results": results, "source": "YouTube Music"})
    except Exception as e:
        return jsonify({"error": f"Falha na busca: {str(e)}"}), 500


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json(force=True)
    url  = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL inválida"}), 400

    # Suporte para URLs do Spotify
    if "open.spotify.com" in url or "spotify:" in url or data.get("source") == "Spotify":
        track_title = data.get("title", "").strip()
        track_artist = data.get("artist", "").strip()
        yt_url, matched_title = resolve_spotify_to_youtube(track_title, track_artist)
        if yt_url:
            url = yt_url

    try:
        # Extrai info básica sem baixar vídeos
        info_ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "js_runtimes": {"node": {}},
            "remote_components": ["ejs:github"],
        }
        if COOKIE_PATH.exists():
            info_ydl_opts["cookiefile"] = str(COOKIE_PATH)
        else:
            info_ydl_opts["extractor_args"] = {"youtube": {"player_client": ["visionos", "android"]}}

        with yt_dlp.YoutubeDL(info_ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        is_playlist = info.get("_type") == "playlist" or "entries" in info

        # Formatos padrão de áudio
        default_audio_formats = [
            {
                "format_id": "bestaudio/best",
                "label": f"MP3 {kbps}kbps",
                "height": 0,
                "fps": 0,
                "ext": "mp3",
                "filesize": None,
                "tbr": kbps,
                "has_audio": True,
                "audio_only": True,
                "mp3_quality": str(kbps),
            }
            for kbps in [320, 256, 192, 128]
        ]

        if is_playlist:
            raw_entries = list(info.get("entries", []) or [])
            entries = []
            for idx, e in enumerate(raw_entries, start=1):
                if not e:
                    continue
                v_id = e.get("id") or ""
                v_url = e.get("url") or (f"https://www.youtube.com/watch?v={v_id}" if v_id else "")
                thumbnails = e.get("thumbnails") or []
                thumb = e.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else "")
                entries.append({
                    "index": idx,
                    "id": v_id,
                    "url": v_url,
                    "title": e.get("title") or f"Faixa {idx}",
                    "uploader": e.get("uploader") or e.get("channel") or "",
                    "duration": e.get("duration") or 0,
                    "thumbnail": thumb,
                })

            # Formatos sugeridos para playlist (áudios + resoluções comuns)
            playlist_formats = list(default_audio_formats)
            for res, label in [(1080, "1080p"), (720, "720p"), (480, "480p"), (360, "360p")]:
                playlist_formats.append({
                    "format_id": f"bestvideo[height<={res}]+bestaudio/best[height<={res}]/best",
                    "label": f"{label} (Vídeo)",
                    "height": res,
                    "fps": 30,
                    "ext": "mp4",
                    "filesize": None,
                    "tbr": None,
                    "has_audio": True,
                    "audio_only": False,
                })

            playlist_thumb = info.get("thumbnail") or (entries[0].get("thumbnail") if entries else "")

            return jsonify({
                "is_playlist": True,
                "title": info.get("title") or "Playlist",
                "uploader": info.get("uploader") or info.get("channel") or "",
                "entry_count": len(entries),
                "thumbnail": playlist_thumb,
                "entries": entries,
                "formats": playlist_formats,
                "is_music": "music.youtube.com" in url,
            })

        # Caso seja vídeo/música individual
        formats, seen = [], set()
        for f in info.get("formats", []):
            vcodec   = f.get("vcodec", "none")
            acodec   = f.get("acodec", "none")
            height   = f.get("height")
            fps      = f.get("fps")
            if vcodec == "none" or not height:
                continue
            key = (height, int(fps or 30))
            if key in seen:
                continue
            seen.add(key)
            label = f"{height}p" + (f" {int(fps)}fps" if fps and fps > 30 else "")
            formats.append({
                "format_id": f.get("format_id", ""),
                "label":     label,
                "height":    height,
                "fps":       fps,
                "ext":       f.get("ext", "mp4"),
                "filesize":  f.get("filesize") or f.get("filesize_approx"),
                "tbr":       f.get("tbr"),
                "has_audio": acodec != "none",
            })

        formats.extend(default_audio_formats)
        formats.sort(key=lambda x: (x.get("height", 0), x.get("fps", 0)), reverse=True)

        is_music = "music.youtube.com" in url or bool(
            info.get("track") or info.get("artist") or info.get("album")
        )
        artist  = info.get("artist") or info.get("uploader") or ""
        track   = info.get("track")  or info.get("title")    or ""
        album   = info.get("album")  or ""
        release = (info.get("release_year")
                   or (info.get("release_date", "")[:4] if info.get("release_date") else "")
                   or "")

        return jsonify({
            "is_playlist": False,
            "title": info.get("title", "Vídeo"),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration", 0),
            "uploader": info.get("uploader", ""),
            "view_count": info.get("view_count", 0),
            "formats": formats,
            "is_music": is_music,
            "artist": artist,
            "track": track,
            "album": album,
            "release_year": str(release) if release else "",
            "genre": info.get("genre") or "",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def download():
    data        = request.get_json(force=True)
    url         = data.get("url", "").strip()
    format_id   = data.get("format_id", "").strip()
    audio_only  = data.get("audio_only", False)
    mp3_quality = data.get("mp3_quality", "192")

    if not url or not format_id:
        return jsonify({"error": "Parâmetros inválidos"}), 400

    job_id = uuid.uuid4().hex[:10]
    job_dir = DOWNLOAD_DIR / f"job_{job_id}"
    job_dir.mkdir(parents=True, exist_ok=True)

    # Tratamento dedicado para faixas originadas do Spotify
    if "open.spotify.com" in url or "spotify:" in url or data.get("source") == "Spotify":
        track_title = data.get("title", "").strip()
        track_artist = data.get("artist", "").strip()
        track_album = data.get("album", "").strip()
        cover_url = data.get("thumbnail", "").strip()

        yt_url, matched_title = resolve_spotify_to_youtube(track_title, track_artist)
        if not yt_url:
            return jsonify({"error": f"Não foi possível localizar o áudio correspondente para: {track_title}"}), 404

        try:
            filepath = _download_single_item(yt_url, job_dir, format_id, audio_only=True, mp3_quality=mp3_quality)
            _apply_spotify_metadata_to_mp3(filepath, track_title, track_artist, track_album, cover_url)

            return jsonify({
                "success": True,
                "download_url": f"/api/get_file/job_{job_id}/{urllib.parse.quote(filepath.name)}",
                "filename": filepath.name
            })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    try:
        filepath = _download_single_item(url, job_dir, format_id, audio_only, mp3_quality)
        return jsonify({
            "success": True,
            "download_url": f"/api/get_file/job_{job_id}/{urllib.parse.quote(filepath.name)}",
            "filename": filepath.name
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download_playlist_zip", methods=["POST"])
def download_playlist_zip():
    """Baixa todas as faixas selecionadas da playlist e retorna URL direta do ZIP."""
    data        = request.get_json(force=True)
    items       = data.get("items", [])
    format_id   = data.get("format_id", "").strip()
    audio_only  = data.get("audio_only", True)
    mp3_quality = data.get("mp3_quality", "320")
    playlist_name = data.get("playlist_title", "Playlist").strip() or "Playlist"

    if not items or not format_id:
        return jsonify({"error": "Nenhum item selecionado ou formato inválido"}), 400

    job_id = uuid.uuid4().hex[:10]
    job_dir = DOWNLOAD_DIR / f"pl_zip_{job_id}"
    files_dir = job_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    downloaded_files = []
    try:
        for idx, item in enumerate(items, start=1):
            item_url = item.get("url")
            if not item_url:
                continue
            item_dir = files_dir / f"track_{idx}"
            try:
                fp = _download_single_item(item_url, item_dir, format_id, audio_only, mp3_quality)
                safe_name = f"{idx:02d} - {fp.name}"
                dest_path = files_dir / safe_name
                shutil.move(str(fp), str(dest_path))
                downloaded_files.append(dest_path)
            except Exception as e:
                print(f"[Playlist ZIP] Falha no item {idx}: {e}")

        if not downloaded_files:
            return jsonify({"error": "Nenhuma faixa pôde ser baixada com sucesso."}), 500

        safe_zip_title = re.sub(r'[\\/*?:"<>|]', "", playlist_name).strip() or "Playlist"
        zip_path = job_dir / f"{safe_zip_title}.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in downloaded_files:
                zf.write(f, arcname=f.name)

        return jsonify({
            "success": True,
            "download_url": f"/api/get_file/pl_zip_{job_id}/{urllib.parse.quote(zip_path.name)}",
            "filename": zip_path.name
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/get_file/<job_folder>/<filename>")
def get_file(job_folder, filename):
    """Entrega o arquivo final diretamente via HTTP GET padrão (compatível com Android DownloadManager)."""
    clean_filename = urllib.parse.unquote(filename)
    folder = DOWNLOAD_DIR / job_folder

    if not folder.exists():
        if (DOWNLOAD_DIR / f"job_{job_folder}").exists():
            folder = DOWNLOAD_DIR / f"job_{job_folder}"
        elif (DOWNLOAD_DIR / f"pl_zip_{job_folder}").exists():
            folder = DOWNLOAD_DIR / f"pl_zip_{job_folder}"

    file_path = folder / clean_filename
    
    if not file_path.exists():
        # Fallback: procura o primeiro arquivo na pasta
        candidates = [p for p in folder.glob("*") if p.is_file()]
        if candidates:
            file_path = candidates[0]
            clean_filename = file_path.name
        else:
            return jsonify({"error": "Arquivo expirado ou não encontrado"}), 404

    return send_file(file_path, as_attachment=True, download_name=clean_filename)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"YouTube Downloader rodando na porta {port}")
    app.run(debug=False, host="0.0.0.0", port=port)


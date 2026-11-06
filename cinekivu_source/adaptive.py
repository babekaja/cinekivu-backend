"""Static, unencrypted DASH to local DASH/HLS presentations using the same fMP4 bytes.

No media URLs come from the player. Every URL is resolved from a bounded upstream
MPD and remains on the manifest's HTTPS origin and directory. Unsupported MPDs
are rejected explicitly instead of inventing a playable playlist.
"""
from dataclasses import dataclass
import copy
import hashlib
import math
import posixpath
import re
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

MAX_MANIFEST_BYTES = 512 * 1024
MAX_TRACKS = 24
MAX_SEGMENTS = 16000
MAX_ASSETS = 48000
NS = "urn:mpeg:dash:schema:mpd:2011"
ET.register_namespace("", NS)


class ManifestError(ValueError):
    pass


def _children(node, name):
    return [c for c in node if c.tag.rsplit("}", 1)[-1] == name]


def _child(node, name):
    return next(iter(_children(node, name)), None)


def _seconds(value):
    if not value:
        return None
    match = re.fullmatch(r"PT(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?", value)
    if not match or not any(match.groups()):
        raise ManifestError("Durée du manifeste non prise en charge.")
    result = sum(float(v or 0) * scale for v, scale in zip(match.groups(), (3600, 60, 1)))
    if not 0 < result <= 28800:
        raise ManifestError("Durée du manifeste hors limites.")
    return result


def _positive(value, default=1):
    try:
        number = int(value) if value is not None else default
    except (ValueError, TypeError):
        raise ManifestError("Valeur numérique du manifeste invalide.") from None
    if number <= 0:
        raise ManifestError("Valeur numérique du manifeste invalide.")
    return number


def _bounded_url(manifest_url, base, reference):
    url = urljoin(base, reference)
    expected, parsed = urlsplit(manifest_url), urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc.lower() != expected.netloc.lower()
            or parsed.username or parsed.password or parsed.port not in (None, 443)
            or parsed.fragment or any(c in url for c in "\r\n\\")):
        raise ManifestError("Le manifeste référence une autre origine média.")
    path = unquote(parsed.path)
    directory = posixpath.dirname(unquote(expected.path)).rstrip("/") + "/"
    if any(p in (".", "..") for p in path.split("/")) or not path.startswith(directory):
        raise ManifestError("Le manifeste référence un fichier hors de son répertoire média.")
    return url


def _base(manifest_url, nodes):
    base = manifest_url
    for node in nodes:
        choices = _children(node, "BaseURL")
        if len(choices) > 1:
            raise ManifestError("Plusieurs BaseURL ne sont pas encore prises en charge.")
        if choices:
            base = _bounded_url(manifest_url, base, (choices[0].text or "").strip())
    return base


def _expand(template, rep_id, bandwidth, number, timestamp):
    template = template.replace("$$", "\x00")
    values = {"RepresentationID": rep_id, "Bandwidth": bandwidth, "Number": number, "Time": timestamp}
    def substitute(match):
        name, width = match.group(1), match.group(2)
        value = values[name]
        if width and (name == "RepresentationID" or int(width) > 12):
            raise ManifestError("Format de SegmentTemplate non pris en charge.")
        return str(value).zfill(int(width)) if width else str(value)
    result = re.sub(r"\$(RepresentationID|Bandwidth|Number|Time)(?:%0(\d+)d)?\$", substitute, template)
    if "$" in result:
        raise ManifestError("Variable de SegmentTemplate inconnue.")
    return result.replace("\x00", "$")


def _timeline(template, period_duration):
    scale = _positive(template.get("timescale"))
    offset = int(template.get("presentationTimeOffset", "0"))
    timeline = _child(template, "SegmentTimeline")
    if timeline is None:
        duration = _positive(template.get("duration"), 0)
        if period_duration is None:
            raise ManifestError("Durée VOD absente du manifeste.")
        count = math.ceil(period_duration * scale / duration)
        if count > MAX_SEGMENTS:
            raise ManifestError("Trop de fragments dans le manifeste.")
        return scale, offset, [(i * duration, min(duration, period_duration * scale - i * duration)) for i in range(count)]
    entries = _children(timeline, "S")
    result, current = [], 0
    for i, entry in enumerate(entries):
        duration = _positive(entry.get("d"), 0)
        start = int(entry.get("t", current))
        if result and start != current:
            raise ManifestError("Les chronologies DASH discontinues nécessitent un autre lecteur.")
        current = start
        repeat = int(entry.get("r", "0"))
        if repeat == -1:
            next_time = entries[i + 1].get("t") if i + 1 < len(entries) else None
            end = int(next_time) if next_time is not None else (
                period_duration * scale + offset if period_duration is not None else None)
            if end is None:
                raise ManifestError("Une répétition DASH ouverte n'a pas de limite VOD.")
            repeat = math.ceil((end - current) / duration) - 1
        if repeat < 0 or len(result) + repeat + 1 > MAX_SEGMENTS:
            raise ManifestError("Répétition ou nombre de fragments invalide.")
        for _ in range(repeat + 1):
            result.append((current, duration))
            current += duration
    if not result:
        raise ManifestError("Le manifeste ne contient aucun fragment.")
    return scale, offset, result


@dataclass(frozen=True)
class Track:
    key: str
    kind: str
    codec: str
    bandwidth: int
    width: int
    height: int
    language: str
    init: str
    segments: tuple[tuple[str, float], ...]
    start: float

    @property
    def hls_compatible(self):
        if self.kind == "video":
            return self.codec.startswith(("avc1.", "hvc1."))
        return self.codec in ("mp4a.40.2", "mp4a.40.5", "mp4a.40.29", "ac-3", "ec-3")


@dataclass
class Presentation:
    dash: bytes
    playlists: dict[str, bytes]
    assets: dict[str, str]
    tracks: tuple[Track, ...]
    hls_height: int


def build_presentation(manifest: bytes, manifest_url: str) -> Presentation:
    if len(manifest) > MAX_MANIFEST_BYTES or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", manifest, re.I):
        raise ManifestError("Manifeste trop volumineux ou XML non autorisé.")
    try:
        root = ET.fromstring(manifest)
    except ET.ParseError:
        raise ManifestError("Le CDN n'a pas renvoyé un manifeste DASH valide.") from None
    if root.tag != f"{{{NS}}}MPD" or root.get("type", "static") != "static":
        raise ManifestError("Seuls les manifestes DASH VOD statiques sont pris en charge.")
    if any(n.tag.rsplit("}", 1)[-1] == "ContentProtection" for n in root.iter()):
        raise ManifestError("Ce flux est protégé et exige un lecteur et une licence compatibles.")
    periods = _children(root, "Period")
    if len(periods) != 1 or periods[0].get("start", "PT0S") not in ("PT0S", "PT0.0S"):
        raise ManifestError("Ce manifeste utilise une chronologie DASH non prise en charge.")
    period = periods[0]
    period_duration = _seconds(period.get("duration") or root.get("mediaPresentationDuration"))
    assets, tracks, track_elements = {}, [], []
    def asset(url):
        key = hashlib.sha256(url.encode()).hexdigest()[:32] + ".m4s"
        if key not in assets and len(assets) >= MAX_ASSETS:
            raise ManifestError("Trop de fichiers dans le manifeste.")
        assets[key] = url
        return "asset/" + key
    for adaptation in _children(period, "AdaptationSet"):
        for rep in _children(adaptation, "Representation"):
            if len(tracks) >= MAX_TRACKS:
                raise ManifestError("Trop de pistes dans le manifeste.")
            mime = rep.get("mimeType", adaptation.get("mimeType", ""))
            kind = rep.get("contentType", adaptation.get("contentType", mime.split("/")[0]))
            if kind not in ("audio", "video"):
                continue
            nodes = (root, period, adaptation, rep)
            base = _base(manifest_url, nodes)
            codec = rep.get("codecs", adaptation.get("codecs", ""))
            if not re.fullmatch(r"[A-Za-z0-9.,_-]{1,100}", codec):
                raise ManifestError("Codec absent ou invalide dans le manifeste.")
            bandwidth = _positive(rep.get("bandwidth"), 128000 if kind == "audio" else 2000000)
            rep_id = rep.get("id", "")
            templates = [_child(n, "SegmentTemplate") for n in nodes]
            templates = [n for n in templates if n is not None]
            lists = [_child(n, "SegmentList") for n in nodes]
            lists = [n for n in lists if n is not None]
            if templates and lists:
                raise ManifestError("Adressage DASH mixte non pris en charge.")
            if templates:
                template = ET.Element("SegmentTemplate")
                for item in templates:
                    template.attrib.update(item.attrib)
                    if _child(item, "SegmentTimeline") is not None:
                        for old in list(template): template.remove(old)
                        template.append(copy.deepcopy(_child(item, "SegmentTimeline")))
                scale, offset, times = _timeline(template, period_duration)
                number = _positive(template.get("startNumber"))
                init_template, media_template = template.get("initialization"), template.get("media")
                if not init_template or not media_template:
                    raise ManifestError("Initialisation ou modèle de fragments DASH absent.")
                init = asset(_bounded_url(manifest_url, base, _expand(init_template, rep_id, bandwidth, number, times[0][0])))
                segments = tuple((asset(_bounded_url(manifest_url, base,
                    _expand(media_template, rep_id, bandwidth, number + i, timestamp))), duration / scale)
                    for i, (timestamp, duration) in enumerate(times))
            elif lists:
                listing = lists[-1]
                scale, offset, times = _timeline(listing, period_duration)
                init_node = _child(listing, "Initialization")
                urls = _children(listing, "SegmentURL")
                if init_node is None or not init_node.get("sourceURL") or init_node.get("range") or any(n.get("mediaRange") for n in urls):
                    raise ManifestError("Les fragments DASH par plages ne sont pas pris en charge.")
                if len(urls) != len(times):
                    raise ManifestError("Chronologie et fragments DASH incohérents.")
                init = asset(_bounded_url(manifest_url, base, init_node.get("sourceURL")))
                segments = tuple((asset(_bounded_url(manifest_url, base, n.get("media", ""))), duration / scale)
                                 for n, (_, duration) in zip(urls, times))
            else:
                raise ManifestError("Ce mode d'adressage DASH nécessite un autre lecteur.")
            if any(not 0 < duration <= 300 for _, duration in segments):
                raise ManifestError("Durée de fragment invalide.")
            key = f"{kind[0]}{len(tracks)}"
            language = adaptation.get("lang", "und")
            if not re.fullmatch(r"[A-Za-z0-9-]{1,35}", language): language = "und"
            track = Track(key, kind, codec, bandwidth, int(rep.get("width", adaptation.get("width", "0"))),
                          int(rep.get("height", adaptation.get("height", "0"))),
                          language, init, segments, (times[0][0] - offset) / scale)
            tracks.append(track)
            track_elements.append((rep, scale, offset, times, track))
    if not tracks:
        raise ManifestError("Aucune piste audio/vidéo trouvée dans le manifeste.")
    # Rebuild only the supported presentation; discard xlinks and remote metadata/addresses.
    inferred_duration = max((times[-1][0] + times[-1][1] - offset) / scale
                            for _, scale, offset, times, _ in track_elements)
    clean_root = ET.Element(f"{{{NS}}}MPD", type="static", minBufferTime="PT1.5S",
                           profiles="urn:mpeg:dash:profile:isoff-live:2011",
                           mediaPresentationDuration=f"PT{period_duration or inferred_duration:.6f}S")
    clean_period = ET.SubElement(clean_root, f"{{{NS}}}Period", start="PT0S")
    groups = {}
    for _, scale, offset, times, track in track_elements:
        group = (track.kind, track.language)
        if group not in groups:
            groups[group] = ET.SubElement(clean_period, f"{{{NS}}}AdaptationSet", contentType=track.kind,
                                         mimeType=f"{track.kind}/mp4", lang=track.language)
        rep = ET.SubElement(groups[group], f"{{{NS}}}Representation", id=track.key,
                            codecs=track.codec, bandwidth=str(track.bandwidth))
        if track.kind == "video":
            rep.set("width", str(track.width)); rep.set("height", str(track.height))
        listing = ET.SubElement(rep, f"{{{NS}}}SegmentList", timescale=str(scale), presentationTimeOffset=str(offset))
        ET.SubElement(listing, f"{{{NS}}}Initialization", sourceURL=track.init)
        timeline = ET.SubElement(listing, f"{{{NS}}}SegmentTimeline")
        for timestamp, duration in times:
            ET.SubElement(timeline, f"{{{NS}}}S", t=str(timestamp), d=str(max(1, round(duration))))
        for name, _ in track.segments:
            ET.SubElement(listing, f"{{{NS}}}SegmentURL", media=name)
    playlists = {}
    videos = [t for t in tracks if t.kind == "video" and t.hls_compatible]
    audios = [t for t in tracks if t.kind == "audio" and t.hls_compatible]
    all_audio_supported = bool(audios) or not any(t.kind == "audio" for t in tracks)
    compatible = videos + audios
    aligned = compatible and max(t.start for t in compatible) - min(t.start for t in compatible) < 0.5
    if videos and all_audio_supported and aligned:
        # HLS default audio order follows CineKivu's FR > EN > other languages.
        audios.sort(key=lambda a: 0 if a.language.lower().startswith('fr') else
                    1 if a.language.lower().startswith('en') else 2)
        master = ["#EXTM3U", "#EXT-X-VERSION:7"]
        for i, audio in enumerate(audios):
            master.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="Audio {i + 1}",LANGUAGE="{audio.language}",DEFAULT={"YES" if i == 0 else "NO"},AUTOSELECT=YES,URI="track-{audio.key}.m3u8"')
        for video in videos:
            codecs = ",".join(dict.fromkeys([video.codec] + [a.codec for a in audios]))
            line = f'#EXT-X-STREAM-INF:BANDWIDTH={video.bandwidth + max((a.bandwidth for a in audios), default=0)},CODECS="{codecs}"'
            if video.width and video.height: line += f",RESOLUTION={video.width}x{video.height}"
            if audios: line += ',AUDIO="audio"'
            master += [line, f"track-{video.key}.m3u8"]
        playlists["master.m3u8"] = ("\n".join(master) + "\n").encode()
        for track in compatible:
            lines = ["#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-PLAYLIST-TYPE:VOD",
                     f"#EXT-X-TARGETDURATION:{math.ceil(max(d for _, d in track.segments))}",
                     "#EXT-X-MEDIA-SEQUENCE:0", f'#EXT-X-MAP:URI="{track.init}"']
            for name, duration in track.segments:
                lines += [f"#EXTINF:{duration:.6f},", name]
            playlists[f"track-{track.key}.m3u8"] = ("\n".join(lines + ["#EXT-X-ENDLIST"]) + "\n").encode()
    return Presentation(ET.tostring(clean_root, encoding="utf-8", xml_declaration=True), playlists, assets,
                        tuple(tracks), max((t.height for t in videos), default=0) if playlists else 0)

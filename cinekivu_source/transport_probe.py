"""Opt-in bounded transport comparison; never a playback authorization."""
import re

import httpx

from .delivery import _public_https_media_url, is_notice_video


USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
              '(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36')


def _summary(status, headers, sample):
    valid_range = bool(re.fullmatch(r'bytes 0-31/\d+', headers.get('content-range', '')))
    mime = headers.get('content-type', '').split(';', 1)[0].lower()
    return {'http_status': status, 'range_verified': valid_range,
            'sample_bytes': len(sample), 'mp4_verified': bool(
                status == 206 and valid_range and mime in ('video/mp4', 'application/octet-stream')
                and len(sample) == 32 and sample[4:8] == b'ftyp')}


def _httpx_probe(url, headers):
    try:
        with httpx.Client(http2=True, follow_redirects=False, timeout=10) as client:
            with client.stream('GET', url, headers=headers) as response:
                sample = b''
                if response.status_code == 206 and re.fullmatch(r'bytes 0-31/\d+', response.headers.get('content-range', '')):
                    for chunk in response.iter_raw(32):
                        sample += chunk[:32-len(sample)]
                        if len(sample) == 32:
                            break
                return {**_summary(response.status_code, response.headers, sample),
                        'http_version': response.http_version}
    except httpx.HTTPError as error:
        return {'error_type': type(error).__name__, 'mp4_verified': False}


def _curl_probe(url, headers):
    try:
        from curl_cffi import Curl, CurlOpt, CurlInfo, CurlHttpVersion
        from curl_cffi.requests.impersonate import DEFAULT_CHROME
    except ImportError:
        return {'error_type': 'TransportUnavailable', 'mp4_verified': False}
    received, sample = {}, bytearray()
    status, stopped = 0, False

    def receive_header(line):
        nonlocal status
        if line.startswith(b'HTTP/'):
            parts = line.split()
            status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
            received.clear()
        elif b':' in line:
            name, value = line.split(b':', 1)
            if name.lower() in (b'content-type', b'content-range'):
                received[name.lower().decode('ascii')] = value.decode('ascii', 'replace').strip()
        return len(line)

    def receive_body(chunk):
        nonlocal stopped
        # A refusal or an ignored Range must never turn this probe into a
        # buffered film download. Only retain the requested 32-byte range.
        if status != 206 or not re.fullmatch(r'bytes 0-31/\d+', received.get('content-range', '')):
            stopped = True
            return 0
        remaining = 32 - len(sample)
        sample.extend(chunk[:remaining])
        if len(chunk) > remaining:
            stopped = True
            return 0
        return len(chunk)

    handle = Curl()
    try:
        if handle.impersonate(DEFAULT_CHROME, default_headers=False) != 0:
            return {'error_type': 'BrowserProfileUnavailable', 'mp4_verified': False}
        handle.setopt(CurlOpt.URL, url.encode())
        handle.setopt(CurlOpt.HTTPHEADER, [f'{k}: {v}'.encode() for k, v in headers.items()])
        handle.setopt(CurlOpt.FOLLOWLOCATION, 0)
        handle.setopt(CurlOpt.TIMEOUT_MS, 10000)
        handle.setopt(CurlOpt.CONNECTTIMEOUT_MS, 4000)
        handle.setopt(CurlOpt.HEADERFUNCTION, receive_header)
        handle.setopt(CurlOpt.WRITEFUNCTION, receive_body)
        try:
            handle.perform()
        except Exception as error:
            if not stopped:
                return {'error_type': type(error).__name__, 'mp4_verified': False}
        result = _summary(int(handle.getinfo(CurlInfo.RESPONSE_CODE)), received, bytes(sample))
        result['browser_profile'] = DEFAULT_CHROME
        try:
            result['http_version'] = CurlHttpVersion(handle.getinfo(CurlInfo.HTTP_VERSION)).name
        except ValueError:
            pass
        return result
    finally:
        handle.close()


def compare_transports(streams):
    candidates = [s for s in streams if str(s.get('format', '')).lower() == 'mp4']
    if not candidates:
        return {'available': False, 'reason': 'no_mp4_candidate'}
    stream = max(candidates, key=lambda s: s.get('quality_value') or 0)
    url = stream.get('url', '')
    if not _public_https_media_url(url) or is_notice_video(url):
        return {'available': False, 'reason': 'invalid_media_candidate'}
    # Identical, explicit headers isolate the transport. No forged client IP,
    # no provider Authorization, no public input URL, no redirects.
    headers = {'User-Agent': USER_AGENT, 'Referer': 'https://moviebox.ph/',
               'Origin': 'https://moviebox.ph', 'Accept': '*/*',
               'Accept-Encoding': 'identity', 'Range': 'bytes=0-31'}
    return {'available': True, 'requested_bytes': 32, 'same_headers': True,
            'httpx': _httpx_probe(url, headers), 'curl_cffi': _curl_probe(url, headers)}

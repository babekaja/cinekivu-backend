"""Keep protocol refusals useful without publishing origin headers or URLs."""
import httpx
import pytest

from cinekivu_source.delivery import _cdn_refusal


@pytest.mark.parametrize('protocol', [b'HTTP/1.1', b'HTTP/2'])
def test_upgrade_refusal_reports_negotiated_protocol(protocol):
    response = httpx.Response(426, headers={'Upgrade': 'h2'},
                              extensions={'http_version': protocol})
    issue = _cdn_refusal(response, 'cette source vidéo')
    assert protocol.decode() in issue
    assert 'protocole requis h2' in issue


def test_upgrade_refusal_omits_arbitrary_origin_content():
    response = httpx.Response(426, headers={'Upgrade': 'https://origin.example/private?token=secret'})
    issue = _cdn_refusal(response, 'cette source vidéo')
    assert 'HTTP 426' in issue
    assert 'secret' not in issue and 'origin.example' not in issue

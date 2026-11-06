"""Regression for a French edition whose work-level language is still English."""
from cinekivu_audio_editions import discovered_editions, edition_language


def test_explicit_french_edition_has_priority_over_original_language():
    assert edition_language('By Any Means [Version française]', 'English') == 'fr'
    assert edition_language('By Any Means [French Dub]', 'English') == 'fr'
    assert edition_language('By Any Means [English Dub]', 'French') == 'en'
    assert edition_language('The French Connection', 'English') == 'en'
    assert edition_language('By Any Means [VOSTFR]', 'English') == 'en'


def test_search_discovery_labels_the_actual_french_subject_as_french():
    original = {'subjectId': '4116962936374151664', 'title': 'By Any Means',
                'subjectType': 1, 'releaseTime': '2026', 'language': 'English'}
    french = {'subjectId': '5312554627534052280', 'title': 'By Any Means [Version française]',
              'subjectType': 1, 'releaseTime': '2026', 'language': 'English',
              'detailPath': 'by-any-means-version-francaise-GFeZVovyrk6'}
    result = discovered_editions(original['subjectId'], original, [], lambda _: [french])
    choice = result['items'][0]
    assert choice['subjectId'] == '5312554627534052280'
    assert choice['languageCode'] == 'fr'
    assert choice['language'] == 'Français'
    assert choice['detailPath'] == french['detailPath']
    assert result['french_editions'] == [choice]
    assert choice['audioVerified'] is False

from modules.config import load_settings


def test_shipped_settings_yaml_is_valid():
    settings = load_settings("config/settings.yaml")
    assert settings.search.api_base_url.startswith("https://")
    assert settings.matching.weights.keyword > 0
    assert 0 <= settings.matching.score_threshold <= 100

from fastapi.testclient import TestClient

from app.main import api


def test_v3_read_api_is_explicitly_unavailable_when_feature_flag_is_off() -> None:
    with TestClient(api) as client:
        response = client.get("/api/v3/universe/features")
    assert response.status_code == 503
    assert response.json()["detail"] == "V3 is not enabled"


def test_v3_openapi_exposes_feature_and_regime_reads() -> None:
    paths = api.openapi()["paths"]
    assert "/api/v3/universe/features" in paths
    assert "/api/v3/market-regime" in paths

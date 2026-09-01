from fastapi.testclient import TestClient

from app.main import api


def test_v3_read_api_is_explicitly_unavailable_when_feature_flag_is_off() -> None:
    with TestClient(api) as client:
        response = client.get("/api/v3/universe/features")
    assert response.status_code == 503
    assert response.json()["code"] == "V3_UNAVAILABLE"
    assert response.json()["message"] == "V3 is not enabled"


def test_v3_openapi_exposes_feature_and_regime_reads() -> None:
    paths = api.openapi()["paths"]
    assert "/api/v3/universe/features" in paths
    assert "/api/v3/market-regime" in paths
    assert "/api/v3/evidence/{subject_type}/{subject_id}" in paths
    assert "/api/v3/recalls" in paths
    assert "/api/v3/raw-opportunities" in paths
    assert "/api/v3/recalls/misses" in paths
    assert "/api/v3/candidates/comparison-pack" in paths
    assert "/api/v3/portfolio/intraday/{code}" in paths


def test_v3_openapi_exposes_ocr_image_upload() -> None:
    paths = api.openapi()["paths"]
    assert "/api/v3/portfolio/images" in paths

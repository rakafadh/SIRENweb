"""Every error a nurse can trigger comes back as Indonesian, tied to the field."""


def _patient(client):
    return client.post("/api/patients", json={"acvpu": "alert"}).json()["patient"]


def test_range_errors_name_the_field_and_unit(client):
    patient = _patient(client)
    body = client.post(f"/api/patients/{patient['id']}/manual-vitals", json={"spo2": 150, "temp": 20}).json()
    assert body["errors"] == {"spo2": "SpO2 maksimal 100%.", "temp": "Suhu minimal 25 °C."}
    assert body["detail"].startswith("2 isian perlu diperbaiki.")


def test_cross_field_error_is_readable(client):
    patient = _patient(client)
    body = client.patch(f"/api/patients/{patient['id']}", json={"systolic": 90, "diastolic": 120}).json()
    assert body["detail"] == "Diastolik harus lebih rendah dari sistolik."


def test_wrong_types_and_choices(client):
    body = client.post("/api/patients", json={"acvpu": "U", "age": "tiga puluh"}).json()
    assert body["errors"]["acvpu"] == "Pilihan kesadaran tidak dikenal."
    assert body["errors"]["age"] == "Usia harus berupa angka bulat."


def test_too_long_text(client):
    patient = _patient(client)
    body = client.post(f"/api/patients/{patient['id']}/notes", json={"text": "x" * 501}).json()
    assert body["errors"]["text"] == "Catatan terlalu panjang (maksimal 500 karakter)."


def test_empty_note_is_rejected(client):
    patient = _patient(client)
    body = client.post(f"/api/patients/{patient['id']}/notes", json={"text": ""}).json()
    assert body["errors"]["text"] == "Catatan wajib diisi."


def test_unknown_api_path_is_json(client):
    response = client.get("/api/patients/tidak-ada")
    assert response.status_code == 404
    assert response.json()["detail"] == "Pasien tidak ditemukan."
    assert client.get("/api/salah-alamat").json()["detail"] == "Data atau halaman tidak ditemukan."


def test_unknown_page_is_a_helpful_html_page(client):
    response = client.get("/halaman-yang-tidak-ada")
    assert response.status_code == 404
    assert "Halaman tidak ditemukan" in response.text
    assert 'href="/"' in response.text


def test_every_page_renders(client):
    patient = _patient(client)
    for path in ["/monitor", "/history", "/sensors", f"/patients/{patient['id']}"]:
        response = client.get(path)
        assert response.status_code == 200, path
        assert "SIREN" in response.text

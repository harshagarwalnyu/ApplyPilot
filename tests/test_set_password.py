import json

from applypilot import config, set_password


def test_generate_meets_common_ats_rules():
    for _ in range(50):
        pw = set_password.generate()
        assert len(pw) == 20
        assert any(c.islower() for c in pw) and any(c.isupper() for c in pw)
        assert any(c.isdigit() for c in pw) and any(c in set_password.SYMBOLS for c in pw)
        assert not set('"\\\'') & set(pw)


def test_typed_password_saved_and_other_fields_kept(tmp_path, monkeypatch, capsys):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"personal": {"email": "a@b.c", "password": ""}, "other": 1}))
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    answers = iter(["Sup3r-secret", "Sup3r-secret"])
    monkeypatch.setattr(set_password.getpass, "getpass", lambda prompt: next(answers))
    assert set_password.main([]) == 0
    saved = json.loads(profile.read_text())
    assert saved == {"personal": {"email": "a@b.c", "password": "Sup3r-secret"}, "other": 1}
    assert "Sup3r-secret" not in capsys.readouterr().out


def test_mismatch_and_short_save_nothing(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"personal": {"password": ""}}))
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    answers = iter(["Sup3r-secret", "different!"])
    monkeypatch.setattr(set_password.getpass, "getpass", lambda prompt: next(answers))
    assert set_password.main([]) == 1
    monkeypatch.setattr(set_password.getpass, "getpass", lambda prompt: "short")
    assert set_password.main([]) == 1
    assert json.loads(profile.read_text())["personal"]["password"] == ""


def test_domain_password_goes_to_site_credentials(tmp_path, monkeypatch):
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({"personal": {"email": "a@b.c", "password": "default1!"}}))
    monkeypatch.setattr(config, "PROFILE_PATH", profile)
    monkeypatch.setattr(set_password.getpass, "getpass", lambda prompt: "Apple-pass9")
    assert set_password.main(["--domain", "IDMSA.apple.com"]) == 0
    saved = json.loads(profile.read_text())
    assert saved["personal"]["password"] == "default1!"
    assert saved["site_credentials"] == {"idmsa.apple.com": {"email": "a@b.c", "password": "Apple-pass9", "login_method": "email"}}

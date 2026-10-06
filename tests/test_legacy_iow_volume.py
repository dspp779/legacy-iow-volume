from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path


def _load():
    path = (
        Path(__file__).resolve().parents[1]
        / "legacy_iow_volume.py"
    )
    spec = importlib.util.spec_from_file_location("legacy_iow_volume", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


mod = _load()

_SAMPLE = (
    "spSaveSocketMessage:0900000001,oENG,034158,MANU,Ready,0,0,12.6,0.1,34,"
    "0.0,35,0,34,OFF,OPEN,9h46m12s,0h4m39s,10,11010,011,11,20*5C"
)


def test_extracts_cumulative_hours_from_raw_eng() -> None:
    assert mod.extract_eng_hours(_SAMPLE, "0900000001") == ("9h46m12s", "0h4m39s")
    assert mod.duration_to_seconds("9h46m12s") == 35172
    assert mod.volume_m3(35172, Decimal("0.3")) == 10552
    assert mod.volume_m3(60, Decimal("0.3")) == 18


def test_sql_row_and_unchanged_state() -> None:
    row = mod.parse_sqlcmd_output(
        "2026-09-07 10:49:00|" + _SAMPLE + "\n(1 rows affected)\n"
    )
    assert row == ("2026-09-07 10:49:00", _SAMPLE)
    assert mod.parse_sqlcmd_output("") is None
    assert mod.should_upload(None, 35172, "2026-09-07 10:49:00") is True
    previous = {"seconds": 35172, "received_at": "2026-09-07 10:49:00"}
    assert mod.should_upload(previous, 35172, "2026-09-07 10:49:00") is False
    assert mod.should_upload(previous, 35180, "2026-09-07 11:00:00") is True


def test_observation_uses_receive_time() -> None:
    item = mod.observation(
        "11111111-1111-1111-1111-111111111111",
        "2026-09-07 10:49:00",
        10552,
    )
    assert item["TimeStamp"] == "2026-09-07T10:49:00+08:00"
    assert item["Value"] == 10552
    assert item["ValueStatus"] == 0


def test_log_file_keeps_latest_eng(tmp_path: Path) -> None:
    log_dir = tmp_path / "Log"
    log_dir.mkdir()
    (log_dir / "Log_2026_09_07.txt").write_text(
        "[2026-09-07 10:43:41]: " + _SAMPLE + "\n",
        encoding="cp950",
    )
    newer = _SAMPLE.replace("9h46m12s", "9h50m0s")
    (log_dir / "Log_2026_09_08.txt").write_text(
        "[2026-09-08 08:01:00]: " + newer + "\n",
        encoding="cp950",
    )
    received_at, message = mod.latest_log_message(
        log_dir,
        "0900000001",
        datetime(2026, 9, 8, 12, 0),
        3,
    )
    assert received_at == "2026-09-08 08:01:00"
    assert mod.extract_eng_hours(message, "0900000001")[0] == "9h50m0s"


_LOGIN_PAGE = """
<form>
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="abc+def=" />
<input type="hidden" name="__VIEWSTATEGENERATOR" value="C2EE9ABB" />
<input type="hidden" name="__EVENTVALIDATION" value="xyz" />
<input name="tbPasswd" type="password" id="tbPasswd" />
</form>
"""

_DETAIL_PAGE = """
<span id="ContentPlaceHolder1_lblRT">9:46:12</span>
<div>上次資料更新時間：</div>
<div>2026/9/7 10:49</div>
"""


class _Body:
    def __init__(self, url: str, body: str) -> None:
        self._url = url
        self._body = body.encode("utf-8")

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args) -> bool:
        return False


class _FakeSite:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def open(self, request, timeout=30):
        url = request.full_url
        if request.data:
            self.posts.append(request.data.decode("utf-8"))
            return _Body(
                "https://gis.cpem.com.tw/Sys/RealTime/RealTimeMap.aspx",
                "<html>map</html>",
            )
        if url.endswith("/Login.aspx"):
            return _Body(url, _LOGIN_PAGE)
        if "RealTimeInfo.ashx" in url:
            return _Body(
                url,
                json.dumps(
                    [
                        {
                            "pumpNo": "150026",
                            "pumpSeq": 1317,
                            "projectSeq": 70,
                            "socketType": 3,
                        }
                    ]
                ),
            )
        if "RealTimeDetail.aspx" in url:
            assert "q=1317&p=70&t=3" in url
            return _Body(url, _DETAIL_PAGE)
        raise AssertionError(url)


def test_login_form_posts_session_fields() -> None:
    fields = mod.login_form(_LOGIN_PAGE, "單位", "account", "secret")
    assert fields["__VIEWSTATE"] == "abc+def="
    assert fields["__EVENTTARGET"] == "btnLogin"
    assert fields["tbUnit"] == "單位"
    assert fields["tbAccount"] == "account"
    assert fields["tbPasswd"] == "secret"
    assert mod.login_succeeded(
        "https://gis.cpem.com.tw/Login.aspx",
        _LOGIN_PAGE,
    ) is False
    assert mod.login_succeeded(
        "https://gis.cpem.com.tw/Sys/RealTime/RealTimeMap.aspx",
        "<html>map</html>",
    ) is True


def test_detail_page_cumulative_hours() -> None:
    parsed = mod.parse_detail_page(_DETAIL_PAGE)
    assert parsed == ("2026-09-07 10:49:00", 35172, "9:46:12")
    assert mod.volume_m3(parsed[1], Decimal("0.3")) == 10552
    assert mod.parse_detail_page("<html></html>") is None


def test_web_config_does_not_require_phone(tmp_path: Path) -> None:
    path = tmp_path / "pumps.json"
    path.write_text(
        json.dumps(
            {
                "source": "web",
                "pumps": [{"pump_no": "150026", "datastream_id": ""}],
            }
        ),
        encoding="utf-8",
    )
    config = mod.load_config(path)
    assert "phone" not in config["pumps"][0]
    assert config["pumps"][0]["rated_cms"] == Decimal("0.3")
    path.write_text(
        json.dumps({"source": "sql", "pumps": [{"pump_no": "150026"}]}),
        encoding="utf-8",
    )
    try:
        mod.load_config(path)
    except ValueError as error:
        assert "invalid phone" in str(error)
    else:
        raise AssertionError("sql config without phone should fail")


def test_web_dry_run_uses_session_and_skips_upload(tmp_path: Path, monkeypatch, caplog) -> None:
    monkeypatch.setenv("LEGACY_UNIT", "單位")
    monkeypatch.setenv("LEGACY_ACCOUNT", "account")
    monkeypatch.setenv("LEGACY_PASSWORD", "secret")
    config = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "pumps.example.json"
        ).read_text(encoding="utf-8")
    )
    config["pumps"] = [config["pumps"][0]]
    config["pumps"][0]["datastream_id"] = "11111111-1111-1111-1111-111111111111"
    site = _FakeSite()
    state_path = tmp_path / "state.json"
    with caplog.at_level("INFO"):
        code = mod.run(config, state_path, dry_run=True, opener=site)
    assert code == 0
    assert not state_path.exists()
    assert "pump_no=150026" in caplog.text
    assert "datastream_id=11111111-1111-1111-1111-111111111111" in caplog.text
    assert '"Id": "11111111-1111-1111-1111-111111111111"' in caplog.text
    assert '"Value": 10552' in caplog.text
    assert "dry-run:" in caplog.text
    assert site.posts
    assert "tbPasswd=secret" in site.posts[0]
    assert "__VIEWSTATE=abc%2Bdef%3D" in site.posts[0]


def test_dry_run_does_not_write_state(tmp_path: Path) -> None:
    config = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "pumps.example.json"
        ).read_text(encoding="utf-8")
    )
    config["source"] = "log"
    config["log_dir"] = str(tmp_path)
    config["pumps"] = [config["pumps"][0]]
    config["pumps"][0]["phone"] = "0900000001"
    config["pumps"][0]["datastream_id"] = "11111111-1111-1111-1111-111111111111"
    (tmp_path / "Log_2026_09_07.txt").write_text(
        "[2026-09-07 10:49:00]: " + _SAMPLE + "\n",
        encoding="cp950",
    )
    state_path = tmp_path / "state.json"
    code = mod.run(
        config,
        state_path,
        dry_run=True,
        now=datetime(2026, 9, 7, 18, 0),
    )
    assert code == 0
    assert not state_path.exists()

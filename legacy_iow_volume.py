"""Upload old-platform 4G hour meters to IoW.

The GitHub Actions workflow logs in to the old website with LEGACY_UNIT,
LEGACY_ACCOUNT, and LEGACY_PASSWORD, keeps the ASP.NET session, then reads
each pump's cumulative run time. Volume is seconds times the configured CMS
rate, rounded half up to whole cubic meters.

source "sql" or "log" remains for a machine that can read CYET or the socket
logs. Those paths use CYET_SQL_USER and CYET_SQL_PASSWORD when Windows auth
is not available. IoW credentials come from IOW_CLIENT_ID and IOW_CLIENT_SECRET.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from zoneinfo import ZoneInfo

_TAIPEI = ZoneInfo("Asia/Taipei")
_HOURS = re.compile(r"(\d+)h(\d+)m(\d+)s", re.IGNORECASE)
_PHONE = re.compile(r"^09\d{8}$")
_LOG_STAMP = re.compile(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]")
_HIDDEN_INPUT = re.compile(r'<input\b[^>]*type="hidden"[^>]*>', re.IGNORECASE)
_INPUT_ATTR = re.compile(r'([\w:]+)="([^"]*)"')
_SITE_TIME = re.compile(
    r"(\d{4})/(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
)
_DEFAULT_SITE = "https://gis.cpem.com.tw"
_DEFAULT_TOKEN_URL = "https://iapi.wra.gov.tw/v3/oauth2/token"
_DEFAULT_WRITE_URL = (
    "https://iapi.wra.gov.tw/v3/api/TimeSeriesData/Write/FormulaTransferred"
)

logger = logging.getLogger("legacy_iow_volume")


def duration_to_seconds(raw: str) -> int | None:
    match = _HOURS.fullmatch(raw.strip())
    if match is None:
        return None
    hours, minutes, seconds = (int(match.group(index)) for index in (1, 2, 3))
    return hours * 3600 + minutes * 60 + seconds


def volume_m3(seconds: int, rated_cms: Decimal | float | str) -> int:
    cubic_meters = Decimal(seconds) * Decimal(str(rated_cms))
    return int(cubic_meters.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def extract_eng_hours(message: str, phone: str) -> tuple[str, str] | None:
    """Return cumulative and trip hour text from one raw ENG message."""
    marker = f"{phone},oENG,"
    start = message.find(marker)
    if start < 0:
        return None
    body = message[start:]
    found = _HOURS.findall(body)
    if not found:
        return None
    total = f"{int(found[0][0])}h{int(found[0][1])}m{int(found[0][2])}s"
    if len(found) == 1:
        return total, ""
    trip = f"{int(found[1][0])}h{int(found[1][1])}m{int(found[1][2])}s"
    return total, trip


def taipei_timestamp(received_at: str) -> str:
    parsed = datetime.strptime(received_at.strip(), "%Y-%m-%d %H:%M:%S")
    return parsed.replace(tzinfo=_TAIPEI).isoformat(timespec="seconds")


def clock_to_seconds(raw: str) -> int | None:
    parts = raw.strip().split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    hours, minutes, seconds = (int(part) for part in parts)
    if minutes > 59 or seconds > 59:
        return None
    return hours * 3600 + minutes * 60 + seconds


def normalize_site_time(raw: str) -> str | None:
    match = _SITE_TIME.search(raw)
    if match is None:
        return None
    year, month, day, hour, minute, second = match.groups()
    second = second or "00"
    return (
        f"{int(year):04d}-{int(month):02d}-{int(day):02d} "
        f"{int(hour):02d}:{int(minute):02d}:{int(second):02d}"
    )


def hidden_fields(html: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for tag in _HIDDEN_INPUT.findall(html):
        attrs = dict(_INPUT_ATTR.findall(tag))
        name = attrs.get("name")
        if name:
            fields[name] = attrs.get("value", "")
    return fields


def login_form(html: str, unit: str, account: str, password: str) -> dict[str, str]:
    fields = hidden_fields(html)
    fields["__EVENTTARGET"] = "btnLogin"
    fields["__EVENTARGUMENT"] = ""
    fields["tbUnit"] = unit
    fields["tbAccount"] = account
    fields["tbPasswd"] = password
    return fields


def login_succeeded(final_url: str, html: str) -> bool:
    if "Login.aspx" in final_url:
        return False
    if 'id="tbPasswd"' in html:
        return False
    return True


def html_lines(html: str) -> list[str]:
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "\n", text)
    lines: list[str] = []
    for line in text.split("\n"):
        cleaned = re.sub(r"\s+", " ", line).replace("&nbsp;", " ").strip()
        if cleaned and cleaned != "～":
            lines.append(cleaned)
    return lines


def line_after(lines: list[str], label: str) -> str:
    for index, line in enumerate(lines):
        if line == label or line.startswith(label):
            if line == label or line.rstrip("：:") == label.rstrip("：:"):
                if index + 1 < len(lines):
                    return lines[index + 1]
            remainder = line[len(label):].strip(" ：:")
            if remainder:
                return remainder
    return ""


def parse_detail_page(html: str) -> tuple[str, int, str] | None:
    lines = html_lines(html)
    span = re.search(
        r'id="ContentPlaceHolder1_lblRT"[^>]*>\s*([^<]+?)\s*</span>',
        html,
        re.IGNORECASE,
    )
    clock = span.group(1).strip() if span else line_after(lines, "累計運轉時間")
    seconds = clock_to_seconds(clock)
    if seconds is None:
        return None
    updated = line_after(lines, "上次資料更新時間：")
    if not updated:
        updated = line_after(lines, "上次資料更新時間")
    received_at = normalize_site_time(updated)
    if received_at is None:
        return None
    return received_at, seconds, clock


def build_opener() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def open_text(opener, url: str, data: bytes | None = None) -> tuple[str, str]:
    headers = {"User-Agent": "legacy-iow-volume"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=data, headers=headers)
    with opener.open(request, timeout=30) as response:
        body = response.read().decode("utf-8", errors="replace")
        return response.geturl(), body


def login(opener, base_url: str, unit: str, account: str, password: str) -> None:
    login_url = base_url.rstrip("/") + "/Login.aspx"
    _, page = open_text(opener, login_url)
    payload = urllib.parse.urlencode(login_form(page, unit, account, password)).encode("utf-8")
    final_url, html = open_text(opener, login_url, data=payload)
    if not login_succeeded(final_url, html):
        raise RuntimeError("old platform login failed")


def fetch_pump_index(opener, base_url: str) -> dict[str, dict]:
    url = base_url.rstrip("/") + "/Api/RealTimeInfo.ashx?q=pump"
    final_url, body = open_text(opener, url)
    if "Login.aspx" in final_url:
        raise RuntimeError("old platform session expired")
    rows = json.loads(body)
    if not isinstance(rows, list):
        raise RuntimeError("pump list was not a JSON array")
    return {str(row.get("pumpNo", "")).strip(): row for row in rows}


def _site_int(value, label: str) -> int:
    text = str(value).strip()
    if not text.isdigit():
        raise ValueError(f"invalid {label}")
    return int(text)


def fetch_detail_page(opener, base_url: str, row: dict) -> str:
    pump_seq = _site_int(row.get("pumpSeq"), "pumpSeq")
    project_seq = _site_int(row.get("projectSeq"), "projectSeq")
    socket_type = _site_int(row.get("socketType"), "socketType")
    url = (
        f"{base_url.rstrip('/')}/Sys/RealTime/RealTimeDetail.aspx"
        f"?q={pump_seq}&p={project_seq}&t={socket_type}"
    )
    final_url, html = open_text(opener, url)
    if "Login.aspx" in final_url:
        raise RuntimeError("old platform session expired")
    return html


def open_web_session(config: dict, opener=None):
    web = config.get("web", {})
    base_url = str(web.get("base_url", _DEFAULT_SITE)).rstrip("/")
    unit = os.environ.get("LEGACY_UNIT", "")
    account = os.environ.get("LEGACY_ACCOUNT", "")
    password = os.environ.get("LEGACY_PASSWORD", "")
    if not unit or not account or not password:
        raise RuntimeError("set LEGACY_UNIT, LEGACY_ACCOUNT, and LEGACY_PASSWORD")
    client = opener or build_opener()
    login(client, base_url, unit, account, password)
    return client, base_url, fetch_pump_index(client, base_url)


def observation(datastream_id: str, received_at: str, cubic_meters: int) -> dict:
    return {
        "Id": datastream_id,
        "TimeStamp": taipei_timestamp(received_at),
        "Value": cubic_meters,
        "ValueStatus": 0,
    }


def should_upload(previous: dict | None, seconds: int, received_at: str) -> bool:
    if not previous:
        return True
    return previous.get("seconds") != seconds or previous.get("received_at") != received_at


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    pumps = data.get("pumps")
    if not isinstance(pumps, list) or not pumps:
        raise ValueError("config pumps must be a non-empty list")
    for pump in pumps:
        phone = str(pump.get("phone", "")).strip()
        if _PHONE.fullmatch(phone) is None:
            raise ValueError(f"invalid phone for pump {pump.get('pump_no')}")
        pump["phone"] = phone
        pump["pump_no"] = str(pump.get("pump_no", "")).strip()
        pump["datastream_id"] = str(pump.get("datastream_id", "")).strip()
        pump["rated_cms"] = Decimal(str(pump.get("rated_cms", "0.3")))
    return data


def sql_query(phone: str, lookback_days: int) -> str:
    if _PHONE.fullmatch(phone) is None:
        raise ValueError(f"invalid phone {phone}")
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    return (
        "SET NOCOUNT ON; "
        "SELECT TOP 1 CONVERT(varchar(19), RECEIVE_TIME, 120), "
        "REPLACE(REPLACE(CAST(MESSAGE AS nvarchar(max)), CHAR(13), ' '), CHAR(10), ' ') "
        "FROM SOCKET_LOG "
        f"WHERE RECEIVE_TIME >= DATEADD(day, -{lookback_days}, GETDATE()) "
        f"AND MESSAGE LIKE '%{phone},oENG,%' "
        "ORDER BY RECEIVE_TIME DESC;"
    )


def parse_sqlcmd_output(text: str) -> tuple[str, str] | None:
    for line in text.splitlines():
        cleaned = line.strip()
        if not cleaned or cleaned.startswith("("):
            continue
        received_at, separator, message = cleaned.partition("|")
        if separator != "|" or len(received_at.strip()) < 19:
            continue
        return received_at.strip()[:19], message.strip()
    return None


def fetch_sqlcmd(server: str, database: str, query: str) -> str:
    command = [
        "sqlcmd",
        "-S",
        server,
        "-d",
        database,
        "-W",
        "-y",
        "0",
        "-s",
        "|",
        "-h-1",
        "-Q",
        query,
    ]
    if os.environ.get("CYET_SQL_USER"):
        command[1:1] = ["-U", os.environ["CYET_SQL_USER"]]
    else:
        command[1:1] = ["-E"]
    environment = os.environ.copy()
    password = os.environ.get("CYET_SQL_PASSWORD")
    if password:
        environment["SQLCMDPASSWORD"] = password
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"sqlcmd failed: {detail[:500]}")
    return completed.stdout


def latest_log_message(
    log_dir: Path,
    phone: str,
    now: datetime,
    lookback_days: int,
) -> tuple[str, str] | None:
    latest: tuple[datetime, str, str] | None = None
    for offset in range(lookback_days):
        day = (now - timedelta(days=offset)).date()
        path = log_dir / f"Log_{day.year}_{day.month:02d}_{day.day:02d}.txt"
        if not path.is_file():
            continue
        text = path.read_text(encoding="cp950", errors="replace")
        for line in text.splitlines():
            if f"{phone},oENG," not in line:
                continue
            stamp = _LOG_STAMP.search(line)
            if stamp is None:
                continue
            received = datetime.strptime(stamp.group(1), "%Y-%m-%d %H:%M:%S")
            if latest is None or received > latest[0]:
                latest = (received, stamp.group(1), line)
    if latest is None:
        return None
    return latest[1], latest[2]


def fetch_oauth_token(token_url: str, client_id: str, client_secret: str) -> str:
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("OAuth response missing access_token")
    return token


def post_observations(write_url: str, token: str, observations: list[dict]) -> None:
    request = urllib.request.Request(
        write_url,
        data=json.dumps(observations).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status < 200 or response.status >= 300:
            raise RuntimeError(f"IoW HTTP {response.status}")


def load_state(path: Path) -> dict:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def reading_for_pump(config: dict, pump: dict, now: datetime) -> tuple[str, str] | None:
    source = str(config.get("source", "sql"))
    lookback = int(config.get("sql", {}).get("lookback_days", 45))
    if source == "log":
        log_dir = Path(config["log_dir"])
        return latest_log_message(log_dir, pump["phone"], now, lookback)
    sql = config.get("sql", {})
    query = sql_query(pump["phone"], lookback)
    output = fetch_sqlcmd(
        str(sql.get("server", ".")),
        str(sql.get("database", "CYET")),
        query,
    )
    return parse_sqlcmd_output(output)


def run(
    config: dict,
    state_path: Path,
    dry_run: bool,
    now: datetime | None = None,
    opener=None,
) -> int:
    moment = now or datetime.now(_TAIPEI)
    state = load_state(state_path)
    source = str(config.get("source", "sql"))
    web_client = None
    base_url = ""
    pump_index: dict[str, dict] = {}
    if source == "web":
        web_client, base_url, pump_index = open_web_session(config, opener)
    pending: list[dict] = []
    uploaded: dict[str, dict] = {}
    for pump in config["pumps"]:
        pump_no = pump["pump_no"]
        datastream_id = pump["datastream_id"]
        if not datastream_id:
            logger.warning("skip pump %s: datastream_id is empty", pump_no)
            continue
        if source == "web":
            row = pump_index.get(pump_no)
            if row is None:
                logger.warning("skip pump %s: not in the realtime list", pump_no)
                continue
            parsed = parse_detail_page(fetch_detail_page(web_client, base_url, row))
            if parsed is None:
                logger.warning("skip pump %s: detail page has no cumulative run time", pump_no)
                continue
            received_at, seconds, shown = parsed
        else:
            found = reading_for_pump(config, pump, moment)
            if found is None:
                logger.warning("skip pump %s: no ENG message in lookback window", pump_no)
                continue
            received_at, message = found
            hours = extract_eng_hours(message, pump["phone"])
            if hours is None:
                logger.warning("skip pump %s: ENG message has no run time", pump_no)
                continue
            seconds = duration_to_seconds(hours[0])
            if seconds is None:
                logger.warning("skip pump %s: unparsable run time %s", pump_no, hours[0])
                continue
            shown = hours[0]
        previous = state.get(pump_no)
        if not should_upload(previous, seconds, received_at):
            logger.info("skip pump %s: reading unchanged", pump_no)
            continue
        cubic_meters = volume_m3(seconds, pump["rated_cms"])
        logger.info(
            "pump %s phone %s run %s volume %s m3 at %s",
            pump_no,
            pump["phone"],
            shown,
            cubic_meters,
            received_at,
        )
        pending.append(observation(datastream_id, received_at, cubic_meters))
        uploaded[pump_no] = {
            "seconds": seconds,
            "received_at": received_at,
            "volume_m3": cubic_meters,
        }
    if not pending:
        logger.info("nothing to upload")
        return 0
    if dry_run:
        logger.info("dry-run: %s observation(s) not sent", len(pending))
        return 0
    iow = config.get("iow", {})
    client_id = os.environ.get("IOW_CLIENT_ID", "")
    client_secret = os.environ.get("IOW_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        raise RuntimeError("set IOW_CLIENT_ID and IOW_CLIENT_SECRET")
    token = fetch_oauth_token(
        str(iow.get("token_url", _DEFAULT_TOKEN_URL)),
        client_id,
        client_secret,
    )
    post_observations(str(iow.get("write_url", _DEFAULT_WRITE_URL)), token, pending)
    state.update(uploaded)
    save_state(state_path, state)
    logger.info("uploaded %s observation(s)", len(pending))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config = load_config(args.config)
    state_path = args.state or args.config.with_name("state.json")
    try:
        return run(config, state_path, args.dry_run)
    except (OSError, urllib.error.URLError, RuntimeError, ValueError) as error:
        logger.error("%s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())

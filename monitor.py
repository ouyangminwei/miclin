"""Run with python monitor.py. Results are saved in the adjacent report directory."""
import csv
import io
import json
import logging
import msvcrt
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from sources import TW, iso_days, scan_inline, scan_mirror, scan_sevenrooms, scan_tablecheck

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "report"
PARTIES = (2, 4, 6)
DAYS = 14
LABELS = {"inline": "inline Dining", "tablecheck": "TableCheck", "sevenrooms": "SevenRooms",
          "mirror": "Noline 備援", "manual": "人工確認"}


def source(row):
    host = urlsplit(row["url"]).hostname
    return {"inline.app": "inline", "www.tablecheck.com": "tablecheck",
            "www.sevenrooms.com": "sevenrooms"}.get(host, "manual")


def collect(group, rows, days, reports):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge")
            context = browser.new_context(locale="en-US" if group != "inline" else "zh-TW",
                                          timezone_id="Asia/Taipei")
            try:
                if group == "inline":
                    scan_inline(context, rows, days, reports)
                else:
                    scan = scan_tablecheck if group == "tablecheck" else scan_sevenrooms
                    for row in rows:
                        for party, party_reports in reports.items():
                            try:
                                scan(context, row, party, days, party_reports[row["name"]])
                            except Exception as exc:
                                party_reports[row["name"]]["note"] = str(exc).splitlines()[0][:160]
                                logging.exception("%s · %s 位", row["name"], party)
            finally:
                browser.close()
    except Exception as exc:
        logging.exception("%s", group)
        for row in rows:
            for party_reports in reports.values():
                party_reports[row["name"]]["note"] = str(exc).splitlines()[0][:160]


def save_history(reports, finished):
    fresh = set()
    db = sqlite3.connect(OUT / "history.sqlite3")
    try:
        with db:
            db.execute("BEGIN IMMEDIATE")
            schema = "at TEXT, party INTEGER, data TEXT, PRIMARY KEY(at, party)"
            keys = [c[1] for c in db.execute("PRAGMA table_info(runs)") if c[5]]
            if keys == ["at"]:
                db.execute(f"CREATE TABLE runs_by_party ({schema})")
                db.execute("INSERT INTO runs_by_party SELECT at, party, data FROM runs")
                db.execute("DROP TABLE runs")
                db.execute("ALTER TABLE runs_by_party RENAME TO runs")
            else:
                db.execute(f"CREATE TABLE IF NOT EXISTS runs ({schema})")
            db.execute("CREATE TABLE IF NOT EXISTS observations (name TEXT, party INTEGER, day TEXT, times TEXT, "
                       "PRIMARY KEY(name, party, day))")
            for party, party_reports in reports.items():
                for name, r in party_reports.items():
                    for day, result in r["days"].items():
                        old = db.execute("SELECT times FROM observations WHERE name=? AND party=? AND day=?",
                                         (name, party, day)).fetchone()
                        if old:
                            for clock in set(result["times"]) - set(json.loads(old[0])):
                                fresh.add((party, name, day, clock))
                        db.execute("INSERT OR REPLACE INTO observations VALUES (?,?,?,?)",
                                   (name, party, day, json.dumps(result["times"])))
                db.execute("INSERT INTO runs VALUES (?,?,?)", (finished, party, json.dumps(party_reports, ensure_ascii=False)))
    finally:
        db.close()
    return fresh


def party_section(party, reports, days, fresh, writer):
    table = []
    available = total = checked = 0
    status_names = {"booking-off": "未開放線上訂位", "closed": "未開放", "full": "平台顯示額滿",
                    "date_unselectable": "平台跳過此日", "selectable": "未列出可選時段"}
    for r in reports.values():
        name = r["name"]
        provider = LABELS[r["source"]]
        link = (f'<a href="{escape(r["url"], quote=True)}">前往訂位或官網</a>' if r["url"] else "")
        if r["phone"]:
            link += f' <span>{escape(r["phone"])}</span>'
        detail = []
        times = []
        for day in days:
            data = r["days"].get(day)
            slots = data["times"] if data else []
            if slots:
                status = "可選時段，方案待確認" if r["source"] == "tablecheck" else "可訂時段"
            elif data:
                status = status_names.get(data["status"], "未列出此人數時段")
            else:
                status = "人工確認" if r["source"] == "manual" else "未取得資料"
            for clock in slots or [""]:
                new = (party, name, day, clock) in fresh
                writer.writerow([name, r["stars"], r["city"], party, day, clock, status, provider,
                                 r["checked_at"] if data else "", r["url"], r["phone"], "新增" if new else ""])
            stamps = [f'<strong>{t} 新增</strong>' if (party, name, day, t) in fresh else t for t in slots]
            detail.append(f'<li><time>{day}</time> {"　".join(stamps) or status}</li>')
            times.extend(f"{day[5:]} {t}" for t in slots)
        checked += bool(r["days"])
        available += bool(times)
        total += len(times)
        hint = ("可選，方案待確認" if r["source"] == "tablecheck" else "可訂") if times else ""
        coverage = f'{len(r["days"])}/{len(days)} 天'
        summary = (f"{times[0]} 起 · {len(times)} 個{hint}時段" if times else
                   ("查詢日期內未列出時段" if r["days"] else "尚待確認"))
        note = escape(r["note"])
        table.append((not bool(times), times[0] if times else "", -r["stars"],
            f'<tr><td>{"★"*r["stars"]}<br>{escape(name)}</td><td>{r["city"]}</td><td>'
            f'<details><summary>{summary}</summary><ul>{"".join(detail)}</ul></details></td>'
            f'<td>{provider}<br>{coverage}<br><small>{escape(r["checked_at"][11:19])} {note}</small></td><td>{link}</td></tr>'))
    body = "".join(t[-1] for t in sorted(table))
    hidden = "" if party == PARTIES[0] else " hidden"
    return f"""<section data-party="{party}"{hidden}>
<p>名單 {len(reports)} 家，{checked} 家讀到日期資料，{available} 家列出合計 {total} 個 {party} 位時段。</p>
<table><thead><tr><th>餐廳</th><th>城市</th><th>{party} 位時段</th><th>資料來源及覆蓋</th><th>訂位方式</th></tr></thead><tbody>{body}</tbody></table></section>"""


def render(reports, days, finished, fresh, guide):
    csv_text = io.StringIO(newline="")
    writer = csv.writer(csv_text)
    writer.writerow(["餐廳", "星數", "城市", "人數", "日期", "時間", "結果", "來源", "查詢時間", "訂位連結", "電話", "新增"])
    sections = "".join(party_section(party, reports[party], days, fresh, writer) for party in PARTIES)
    options = "".join(f'<option value="{party}">{party} 位</option>' for party in PARTIES)
    html = f"""<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>台灣星級餐廳空位</title><style>
body{{margin:2rem auto;padding:0 1rem;max-width:1100px;font:16px/1.55 system-ui;color:#182929}}
h1{{font-size:1.8rem}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:.7rem;border-bottom:1px solid #ddd;vertical-align:top}}
th{{background:#eef4f0}}summary,a,strong{{color:#126650}}summary{{cursor:pointer}}ul{{padding-left:1rem}}li{{margin:.3rem 0}}time{{font-variant-numeric:tabular-nums}}small{{color:#555}}section{{overflow-x:auto}}
</style><h1>台灣星級餐廳空位</h1><p>{days[0]} 至 {days[-1]} · 台灣時間 {finished[:19].replace("T"," ")}</p>
<p>TableCheck 列出可選時間，方案及座位需進訂位頁確認。「新增」表示同一家餐廳、日期及人數相較前次成功查詢新出現的時段。</p>
<p><a href="availability.csv">下載所有人數的逐日 CSV</a> · <a href="{escape(guide)}">2026 米其林名單</a></p>
<label>用餐人數 <select id="party">{options}</select></label>
{sections}
<script>
const party = document.getElementById("party");
function showParty() {{
  for (const section of document.querySelectorAll("section[data-party]")) {{
    section.hidden = section.dataset.party !== party.value;
  }}
}}
party.addEventListener("change", showParty);
showParty();
</script></html>"""
    for name, content, encoding in [("availability.csv", csv_text.getvalue(), "utf-8-sig"), ("index.html", html, "utf-8")]:
        tmp = OUT / (name + ".tmp")
        tmp.write_bytes(content.encode(encoding))
        tmp.replace(OUT / name)


def run():
    catalog = json.loads((ROOT / "restaurants.json").read_text(encoding="utf-8"))
    days = iso_days(datetime.now(TW).date(), DAYS)
    reports = {party: {} for party in PARTIES}
    groups = {}
    for row in catalog["restaurants"]:
        group = source(row)
        note = ""
        if group == "manual":
            note = "預約需驗證電子郵件" if row["name"] == "盈科" else "請向餐廳確認"
        for party_reports in reports.values():
            party_reports[row["name"]] = dict(row, source=group, days={}, checked_at="", note=note)
        if group != "manual":
            groups.setdefault(group, []).append(row)
    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = []
        for group, rows in groups.items():
            batches = [rows[::2], rows[1::2]] if group == "tablecheck" else [rows]
            jobs.extend(pool.submit(collect, group, batch, days, reports) for batch in batches if batch)
        for job in jobs:
            job.result()
        def fallback(row):
            try:
                missing = {party: party_reports[row["name"]] for party, party_reports in reports.items()
                           if not party_reports[row["name"]]["days"]}
                scan_mirror(row, days, missing)
            except Exception:
                logging.info("無法讀取備援日曆 · %s", row["name"], exc_info=True)
        missing = [r for r in catalog["restaurants"] if "mirror" in r
                   and any(not party_reports[r["name"]]["days"] for party_reports in reports.values())]
        list(pool.map(fallback, missing))
    finished_at = datetime.now(TW)
    for party_reports in reports.values():
        for r in party_reports.values():
            for key, d in r["days"].items():
                d["times"] = [t for t in d["times"] if key + "T" + t > finished_at.strftime("%Y-%m-%dT%H:%M")]
    stamp = finished_at.isoformat(timespec="seconds")
    fresh = save_history(reports, stamp)
    render(reports, days, stamp, fresh, catalog["guide"])
    counts = {party: sum(bool(r["days"]) for r in rows.values()) for party, rows in reports.items()}
    logging.info("完成 · 各人數取得日期資料的餐廳數 %s · %s 個新增時段", counts, len(fresh))
    return 0 if all(counts.values()) else 1


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    logging.basicConfig(filename=OUT / "monitor.log", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    with (OUT / "monitor.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("已有查詢執行中")
        try:
            raise SystemExit(run())
        except Exception:
            logging.exception("未完成本次報告")
            raise

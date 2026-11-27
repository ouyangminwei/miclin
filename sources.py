"""Read inventory shown by Taiwan reservation websites in their public guest UI."""
import json
import re
import logging
from html.parser import HTMLParser
from urllib.request import urlopen
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.sync_api import Error as BrowserError

TW = timezone(timedelta(hours=8))
DINING = "https://dining.inline.app/zh-tw/discover/awards-winning/michelin/"
SLOT_PATH = "https://diner-network-api.inline.app/diner-network/discover/bookable-timeslots"


def iso_days(first, count):
    return [(first + timedelta(days=i)).isoformat() for i in range(count)]


class MirrorPage(HTMLParser):
    """Read the same calendar and update timestamp delivered with the public page."""

    def __init__(self, slug):
        super().__init__()
        self.slug = slug
        self.calendar = None

    def handle_data(self, text):
        if not text.startswith("self.__next_f.push(") or "lastFetchedAt" not in text:
            return
        parts, _ = json.JSONDecoder().raw_decode(text[len("self.__next_f.push("):])
        for line in parts[1].splitlines():
            _, sep, value = line.partition(":")
            if sep and value.startswith("["):
                self.visit(json.loads(value))

    def visit(self, value):
        if isinstance(value, dict):
            if value.get("restaurantId") == self.slug and "days" in value and "lastFetchedAt" in value:
                self.calendar = value
            else:
                for child in value.values():
                    self.visit(child)
        if isinstance(value, list):
            for child in value:
                self.visit(child)


def scan_mirror(row, days, reports):
    page = MirrorPage(row["mirror"])
    with urlopen("https://www.noline.tw/restaurant/" + row["mirror"], timeout=12) as response:
        page.feed(response.read().decode("utf-8"))
    calendar = page.calendar
    if not calendar or not calendar["lastFetchedAt"]:
        return
    if urlsplit(calendar["bookingUrl"]).path.rstrip("/") != urlsplit(row["url"]).path.rstrip("/"):
        return
    observed = datetime.fromisoformat(calendar["lastFetchedAt"].replace("Z", "+00:00")).astimezone(TW)
    now = datetime.now(TW)
    if not timedelta() <= now - observed <= timedelta(hours=1):
        for report in reports.values():
            report["note"] = f"備援上次同步 {observed:%m/%d %H:%M}，已逾 1 小時"
        return
    for party, report in reports.items():
        apply_mirror_calendar(calendar, party, days, report, observed, now)


def apply_mirror_calendar(calendar, party, days, report, observed, now):
    merged = {}
    for entry in calendar["days"]:
        for key, day in read_inline({"result": [entry]}, party, days, now).items():
            # The same day may have separate seating groups. Keep slots from every group.
            old = merged.get(key)
            if old:
                day["times"] = sorted(set(old["times"]) | set(day["times"]))
                if old["status"] == "open":
                    day["status"] = "open"
            merged[key] = day
    if merged:
        report.update(days=merged, source="mirror", checked_at=observed.isoformat(timespec="seconds"),
                      note="公開備援日曆，時間為來源同步時間")


def read_inline(payload, party, days, now):
    result = {}
    for day in payload["result"]:
        key = day["date"]
        if key not in days:
            continue
        status = day.get("status")
        times = day.get("times")
        if not isinstance(times, dict):
            continue
        slots = set()
        if status == "open":
            for clock, sizes in times.items():
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock) or not isinstance(sizes, list):
                    raise ValueError("時段格式有變動")
                if party in sizes and key + "T" + clock > now.strftime("%Y-%m-%dT%H:%M"):
                    slots.add(clock)
        elif status not in ("full", "booking-off", "closed"):
            continue
        result[key] = {"status": status, "times": sorted(slots)}
    return result


def scan_inline(context, restaurants, days, reports):
    by_branch = {urlsplit(r["url"]).path.rstrip("/").split("/")[-1]: r["name"] for r in restaurants}
    page = context.new_page()
    seen = set()

    def receive(response):
        if response.url.split("?")[0] != SLOT_PATH or not response.ok:
            return
        try:
            data = response.json()
            name = by_branch.get(data.get("branchId"))
            if name:
                observed = datetime.now(TW)
                for party, party_reports in reports.items():
                    data_days = read_inline(data, party, days, observed)
                    if data_days:
                        party_reports[name]["days"].update(data_days)
                        party_reports[name]["checked_at"] = observed.isoformat(timespec="seconds")
                        seen.add(name)
        except (BrowserError, ValueError, TypeError, KeyError):
            return

    page.on("response", receive)
    try:
        page.goto(DINING, wait_until="domcontentloaded", timeout=35000)
        cards = page.locator("h2.cursor-pointer")
        cards.first.wait_for(timeout=20000)
        index = 0
        while index < 85 and len(seen) < len(by_branch):
            count = cards.count()
            if index >= count:
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                try:
                    cards.nth(index).wait_for(state="attached", timeout=3500)
                except BrowserError:
                    break
                continue
            card = cards.nth(index)
            card.scroll_into_view_if_needed(timeout=5000)
            page.wait_for_timeout(360)
            index += 1
        page.wait_for_timeout(1400)
        for row in restaurants:
            if row["name"] in seen:
                continue
            name = row["name"]
            query = {"A": "Restaurant A", "承": "承 Sho", "掬": "Kiku"}.get(name, name)
            url = "https://dining.inline.app/zh-tw/search?" + urlencode({"q": query})
            try:
                with page.expect_response(lambda r: urlsplit(r.url).path == "/diner-network/search", timeout=6000):
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                target = page.locator(f'a[href*="{urlsplit(row["url"]).path.split("/")[-1]}"]')
                if target.count():
                    target.first.scroll_into_view_if_needed(timeout=2500)
                page.wait_for_timeout(1000)
            except BrowserError:
                logging.info("inline 搜尋無結果或逾時 · %s", name)
            if name not in seen:
                for party_reports in reports.values():
                    party_reports[name]["note"] = "公開頁未傳回這間分店的日期庫存"
    finally:
        page.close()


class PartySizeNotOffered(Exception):
    pass


def tablecheck_day(page, restaurant, key, party):
    path = urlsplit(restaurant["url"]).path.split("/")
    shop = path[path.index("shops") + 1] if "shops" in path else path[2]
    url = f"https://www.tablecheck.com/en/{shop}/reserve/landing?"
    url += urlencode({"start_date": key, "num_people": party})

    def matches(response):
        if not response.url.endswith("/booking/meals_v2"):
            return False
        query = response.request.post_data_json or {}
        return query.get("shop_id") == shop and query.get("pax_adult") == party

    with page.expect_response(matches, timeout=16000) as event:
        page.goto(url, wait_until="domcontentloaded", timeout=22000)
        policy = page.locator("button:visible").filter(has_text=re.compile("^Confirm and continue$", re.I)).last
        time_button = page.get_by_role("button", name="Select a time", exact=True).first
        policy.or_(time_button).first.wait_for(state="attached", timeout=8000)
        if policy.is_visible():
            policy.click(timeout=4000)
        guests = page.get_by_role("button", name=re.compile(r"^\d+ Guests?$")).first
        if guests.is_visible() and guests.inner_text().split()[0] != str(party):
            guests.click(timeout=4000)
            selector = page.locator('select[data-testid="Counter Select"]:visible')
            if selector.count() == 1:
                values = selector.locator("option:enabled").evaluate_all("(items) => items.map(item => Number(item.value))")
                if values and party not in values:
                    raise PartySizeNotOffered(f"訂位頁人數選單最高 {max(values)} 位，{party} 位請洽餐廳")
                selector.select_option(str(party))
            page.get_by_role("button", name="Confirm", exact=True).last.click(timeout=4000)
        if time_button.is_visible():
            time_button.click(timeout=4000)
    response = event.value
    if not response.ok:
        if response.status == 422 and page.get_by_text("No dates available", exact=True).is_visible():
            return key, {"status": "date_unselectable", "times": []}
        raise ValueError(f"HTTP {response.status}")
    actual = date.fromisoformat(response.request.post_data_json["date"][:10]).isoformat()
    meals = response.json()["meals"]
    if not isinstance(meals, dict):
        raise ValueError("時段格式有變動")
    slots = set()
    for meal in meals.values():
        for item in meal:
            if not isinstance(item.get("a"), bool):
                raise ValueError("時段狀態有變動")
            instant = datetime.fromisoformat(item["t"].replace("Z", "+00:00")).astimezone(TW)
            if item["a"] and instant.date().isoformat() == actual and instant > datetime.now(TW):
                slots.add(instant.strftime("%H:%M"))
    return actual, {"status": "selectable", "times": sorted(slots)}


def scan_tablecheck(context, restaurant, party, days, report):
    page = context.new_page()
    try:
        failures = 0
        for key in days:
            if key in report["days"]:
                continue
            try:
                actual, result = tablecheck_day(page, restaurant, key, party)
                if actual in days:
                    report["days"][actual] = result
                if actual != key:
                    report["days"][key] = {"status": "date_unselectable", "times": []}
                report["checked_at"] = datetime.now(TW).isoformat(timespec="seconds")
                failures = 0
            except PartySizeNotOffered as exc:
                report["note"] = str(exc)
                break
            except (BrowserError, ValueError, TypeError, KeyError) as exc:
                report["note"] = f"{key} 查詢失敗 · {str(exc).splitlines()[0][:120]}"
                failures += 1
                if failures >= 2:
                    break
    finally:
        page.close()


def scan_sevenrooms(context, restaurant, party, days, report):
    page = context.new_page()
    try:
        for key in days:
            target = date.fromisoformat(key)
            page.goto(restaurant["url"], wait_until="domcontentloaded", timeout=22000)
            guest_button = page.locator('[data-test="sr-guest-count-button"]')
            guest_button.wait_for(timeout=8000)
            if guest_button.inner_text().split()[0] != str(party):
                guest_button.click()
                page.get_by_role("button", name=str(party), exact=True).click(timeout=4000)
            page.get_by_text("Date", exact=True).first.click(timeout=8000)
            today = datetime.now(TW).date()
            if (target.year, target.month) != (today.year, today.month):
                page.get_by_role("button", name="increment month").click()
            label = target.strftime("%A, %B ") + str(target.day) + r"(?:st|nd|rd|th) " + str(target.year) + ", Available$"
            cell = page.get_by_label(re.compile(label))
            cell.click(timeout=4500)
            def matches(response):
                url = urlsplit(response.url)
                query = parse_qs(url.query)
                return (url.path.endswith("/availability/widget/range") and query.get("party_size") == [str(party)]
                        and query.get("venue") == ["yagetaipei"] and query.get("start_date") == [target.strftime("%m-%d-%Y")])
            with page.expect_response(matches, timeout=12000) as event:
                page.get_by_text("Search", exact=True).first.click()
            response = event.value
            shifts = response.json()["data"]["availability"][key]
            if not response.ok or not isinstance(shifts, list):
                raise ValueError("SevenRooms 時段格式有變動")
            slots = set()
            for shift in shifts:
                for item in shift["times"]:
                    if item["type"] == "book" and not shift.get("is_closed"):
                        clock = datetime.strptime(item["time"], "%I:%M %p").strftime("%H:%M")
                        if key + "T" + clock > datetime.now(TW).strftime("%Y-%m-%dT%H:%M"):
                            slots.add(clock)
            report["days"][key] = {"status": "book", "times": sorted(slots)}
            report["checked_at"] = datetime.now(TW).isoformat(timespec="seconds")
    finally:
        page.close()

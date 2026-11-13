"""透過 Temi 上的 Java App（Temi SDK）派遣 Temi，並等待 App 回報抵達。

App 端要加入 temi_app/FallResponseServer.java，安裝方式見 temi_app/README.md。
電腦和 Temi 要連同一個 Wi-Fi。

列出 Temi 上的地點：python -m fall_response.temi_app --url http://TemiIP:8080 --list（不會移動）
直接派遣測試：      python -m fall_response.temi_app --url http://TemiIP:8080 --go vr（Temi 會移動）
"""
import argparse
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

ARRIVED = "complete"
ABORTED = "abort"
MIN_REQUEST_TIMEOUT_S = 0.5


class TemiAppError(RuntimeError):
    pass


class TemiAppController:
    """實作 incident.RobotController。"""

    confirms_arrival = True

    def __init__(
        self,
        base_url: str,
        poll_s: float = 0.5,
        request_timeout_s: float = 5.0,
        opener: Callable = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._base_url = base_url.rstrip("/")
        self._poll_s = poll_s
        self._request_timeout_s = request_timeout_s
        self._opener = opener
        self._clock = clock
        self._sleep = sleep

    def locations(self) -> list:
        return list(self._request("GET", "/locations").get("locations", []))

    def prepare(self, locations: Iterable[str]) -> None:
        """啟動時確認所有觀察點都存在 Temi 上，名稱有誤時立刻報錯。"""
        available = self.locations()
        known = {name.lower() for name in available}
        missing = [name for name in locations if name.lower() not in known]
        if missing:
            raise TemiAppError(
                f"Temi 上沒有地點：{'、'.join(missing)}，目前有：{'、'.join(available) or '（無）'}"
            )
        logger.info("已確認 Temi 上的觀察點：%s", "、".join(sorted(set(locations))))

    def dispatch(self, location: str, timeout_s: Optional[float] = None) -> str:
        """只送出派遣，回傳這次派遣的編號。"""
        result = self._request("POST", "/goto", {"location": location}, timeout_s)
        logger.warning("派遣 Temi 前往 %s（Java App）", location)
        return str(result.get("request_id", ""))

    def go_to(self, location: str, timeout_s: float) -> bool:
        """送出派遣後等待 Temi 回報；抵達回傳 True，時間到仍未抵達回傳 False，一定在 timeout_s 內返回。"""
        deadline = self._clock() + timeout_s
        request_id = self.dispatch(location, timeout_s)
        last_status = None
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                logger.error("等待 Temi 抵達 %s 逾時（最後狀態：%s）", location, last_status)
                return False
            self._sleep(min(self._poll_s, remaining))
            remaining = deadline - self._clock()
            if remaining <= 0:
                continue
            state = self._request("GET", "/status", timeout_s=remaining)
            if str(state.get("request_id")) != request_id:
                raise TemiAppError("Temi 收到了另一個派遣指令，這次派遣已被取代")
            status = state.get("status")
            if status != last_status:
                logger.info("Temi 導航狀態：%s %s", status, state.get("description") or "")
                last_status = status
            if status == ARRIVED:
                logger.warning("Temi 已抵達 %s", location)
                return True
            if status == ABORTED:
                raise TemiAppError(f"Temi 導航中止：{state.get('description') or '原因不明'}")

    def stop(self) -> None:
        self._request("POST", "/stop")

    def _request(self, method: str, path: str, body=None, timeout_s: Optional[float] = None) -> dict:
        timeout = self._request_timeout_s if timeout_s is None else min(self._request_timeout_s, timeout_s)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self._base_url + path, data=data, method=method, headers={"Content-Type": "application/json"}
        )
        try:
            with self._opener(request, timeout=max(timeout, MIN_REQUEST_TIMEOUT_S)) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read().decode("utf-8")).get("error")
            except (ValueError, OSError):
                message = None
            raise TemiAppError(f"Temi App {path} 失敗：{message or f'HTTP {error.code}'}") from None
        except urllib.error.URLError as error:
            raise TemiAppError(f"無法連線 Temi App（{self._base_url}）：{error.reason}") from None
        except TimeoutError:
            raise TemiAppError(f"連線 Temi App 逾時（{self._base_url}）") from None
        except ValueError:
            raise TemiAppError(f"Temi App {path} 回傳的不是 JSON") from None


def main() -> None:
    parser = argparse.ArgumentParser(description="測試 Temi Java App 連線與派遣")
    parser.add_argument("--url", required=True, help="Temi App 的網址，例如 http://192.168.50.20:8080")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出 Temi 上的地點，不會移動 Temi")
    group.add_argument("--go", metavar="地點", help="派遣 Temi 並等待抵達（Temi 會移動）")
    group.add_argument("--stop", action="store_true", help="讓 Temi 停止移動")
    parser.add_argument("--timeout", type=float, default=120.0, help="--go 最多等待幾秒")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    controller = TemiAppController(args.url)
    try:
        if args.list:
            for name in controller.locations():
                print(name)
        elif args.stop:
            controller.stop()
            print("已要求 Temi 停止")
        else:
            started = time.monotonic()
            arrived = controller.go_to(args.go, args.timeout)
            elapsed = time.monotonic() - started
            print(f"已抵達，花了 {elapsed:.1f} 秒" if arrived else f"{elapsed:.0f} 秒內沒有抵達")
    except TemiAppError as error:
        logger.error("%s", error)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

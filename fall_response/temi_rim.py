"""透過博鈞 RIM2 API 派遣 Temi：執行事先在 RIM2 網站建立的 eRoute 任務。

RIM2 只會回覆任務是否送到機器人，不會回報 Temi 是否已抵達，
所以送出成功後等待 arrival_wait_s 秒就往下走，並標記為「未確認抵達」。

查看機器人與 eRoute：python -m fall_response.temi_rim --list
確認地點設定：      python -m fall_response.temi_rim --check 客廳（不會移動）
直接派遣測試：      python -m fall_response.temi_rim --go 客廳（Temi 會移動）
"""
import argparse
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Union

logger = logging.getLogger(__name__)

DEFAULT_HOST = "https://rim2.mobisnet.com/rim_api/v1"
DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "temi_config.json"
TOKEN_LIFETIME_S = 90 * 60  # token 兩小時後過期，提早重新登入
MIN_REQUEST_TIMEOUT_S = 1.0


class RimError(RuntimeError):
    pass


class _Unauthorized(Exception):
    pass


@dataclass(frozen=True)
class TemiConfig:
    authsid: str
    username: str
    password: str = field(repr=False)
    routes: dict  # 地點名稱 → eRoute 名稱或 ID
    default_location: str = "客廳"
    robot_name: Optional[str] = None  # 共用帳號下有多台 Temi，正式派遣時必須填寫
    arrival_wait_s: float = 20.0
    host: str = DEFAULT_HOST
    request_timeout_s: float = 30.0


def load_config(path: Union[str, Path] = DEFAULT_CONFIG) -> TemiConfig:
    path = Path(path)
    if not path.exists():
        raise RimError(f"找不到 Temi 設定檔：{path}（請複製 temi_config.example.json 後填寫）")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RimError(f"設定檔格式錯誤：{path} 第 {error.lineno} 行") from None
    missing = [key for key in ("authsid", "username", "password", "routes") if not data.get(key)]
    if missing:
        raise RimError(f"設定檔缺少欄位：{', '.join(missing)}")
    known = TemiConfig.__dataclass_fields__
    return TemiConfig(**{key: value for key, value in data.items() if key in known})


class RimClient:
    def __init__(
        self,
        config: TemiConfig,
        opener: Callable = urllib.request.urlopen,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._config = config
        self._opener = opener
        self._clock = clock
        self._token: Optional[str] = None
        self._token_at = 0.0

    def login(self, timeout_s: Optional[float] = None) -> None:
        body = {"authsid": self._config.authsid, "username": self._config.username, "password": self._config.password}
        data = self._request("POST", "/login", body=body, authorized=False, timeout_s=timeout_s)
        self._token = data["token"]
        self._token_at = self._clock()

    def robots(self) -> list:
        return self._request("GET", "/robot/robots") or []

    def eroutes(self, robot_id: int) -> list:
        return self._request("GET", "/eroute/eroutes", params={"robot_id": robot_id}) or []

    def execute(self, robot_id: int, route_id: int, timeout_s: Optional[float] = None) -> None:
        params = {"robot_id": robot_id, "route_id": route_id}
        self._request("GET", "/eroute/execute", params=params, timeout_s=timeout_s)

    def _request(self, method: str, path: str, params=None, body=None, authorized: bool = True, timeout_s=None):
        """timeout_s 是這次呼叫（含必要的重新登入）的總時限，未指定時用設定檔的 request_timeout_s。"""
        deadline = self._clock() + (timeout_s if timeout_s is not None else self._config.request_timeout_s)
        for attempt in (1, 2):
            headers = {"Content-Type": "application/json"}
            if authorized:
                headers["Authorization"] = "Bearer " + self._valid_token(deadline)
            try:
                return self._send(method, path, params, body, headers, deadline)
            except _Unauthorized:
                if not authorized or attempt == 2:
                    raise RimError(f"RIM2 {path} 驗證失敗，請確認帳號密碼") from None
                # 憑證可能被提前取消（例如共用帳號被別人登入），重新登入後再試一次
                logger.warning("RIM2 登入憑證失效，重新登入")
                self._token = None

    def _send(self, method: str, path: str, params, body, headers: dict, deadline: float):
        url = self._config.host + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        timeout = max(min(self._config.request_timeout_s, deadline - self._clock()), MIN_REQUEST_TIMEOUT_S)
        try:
            with self._opener(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 401:
                raise _Unauthorized() from None
            payload = self._error_payload(error)
        except urllib.error.URLError as error:
            raise RimError(f"無法連線 RIM2：{error.reason}") from None
        except TimeoutError:
            raise RimError("連線 RIM2 逾時") from None

        if payload.get("status") != "success":
            raise RimError(f"RIM2 {path} 失敗：{payload.get('msg')}（code {payload.get('code')}）")
        return payload.get("data")

    def _valid_token(self, deadline: float) -> str:
        if self._token is None or self._clock() - self._token_at > TOKEN_LIFETIME_S:
            self.login(timeout_s=max(deadline - self._clock(), MIN_REQUEST_TIMEOUT_S))
        return self._token

    @staticmethod
    def _error_payload(error: urllib.error.HTTPError) -> dict:
        try:
            return json.loads(error.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"status": "fail", "msg": f"HTTP {error.code}", "code": error.code}


class RimRobotController:
    """實作 incident.RobotController。"""

    confirms_arrival = False

    def __init__(
        self,
        client: RimClient,
        config: TemiConfig,
        sleep: Callable[[float], None] = time.sleep,
        dry_run: bool = False,
        require_robot_name: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._client = client
        self._config = config
        self._sleep = sleep
        self._dry_run = dry_run  # True 時只查詢任務，不送出指令，Temi 不會移動
        self._require_robot_name = require_robot_name  # 正式派遣時避免派到共用帳號下的別台 Temi
        self._clock = clock
        self._robot_id: Optional[int] = None
        self._route_ids: dict = {}

    def prepare(self, locations: Iterable[str]) -> None:
        """啟動時先查好機器人與所有任務的 ID，名稱有誤會立刻報錯，跌倒時也只需送出一次指令。"""
        self.robot_id()
        for location in locations:
            self.route_id(location)
        logger.info("已確認 %d 個觀察點的 eRoute：%s", len(self._route_ids), self._route_ids)

    def go_to(self, location: str, timeout_s: float) -> bool:
        """必須在 timeout_s 內返回，否則流程會判定 Temi 無法到場，背景卻可能仍在派遣。"""
        deadline = self._clock() + timeout_s
        route_id = self.route_id(location)
        if self._dry_run:
            logger.warning("【試跑】將派遣 Temi 前往 %s（eRoute %s），未送出指令，Temi 不會移動", location, route_id)
            return True
        self.dispatch(location, timeout_s=deadline - self._clock())
        # 無法得知何時抵達，只能等待預估的移動時間，但不能超過時限
        self._sleep(max(min(self._config.arrival_wait_s, deadline - self._clock()), 0.0))
        return True

    def dispatch(self, location: str, timeout_s: Optional[float] = None) -> None:
        """只送出任務，不等待抵達。"""
        robot_id, route_id = self.robot_id(), self.route_id(location)
        logger.warning("派遣 Temi 前往 %s（eRoute %s）", location, route_id)
        self._client.execute(robot_id, route_id, timeout_s=timeout_s)

    def robot_id(self) -> int:
        if self._robot_id is not None:
            return self._robot_id
        name = self._config.robot_name
        if not name and self._require_robot_name:
            raise RimError("正式派遣前必須在 temi_config.json 填寫 robot_name，避免派到共用帳號下的別台 Temi")
        robots = self._client.robots()
        if not robots:
            raise RimError("RIM2 帳號下沒有機器人")
        matched = [robot for robot in robots if robot.get("name") == name] if name else robots[:1]
        if not matched:
            available = "、".join(str(robot.get("name")) for robot in robots)
            raise RimError(f"找不到名稱為「{name}」的機器人，目前有：{available}")
        if not name:
            logger.warning("未指定 robot_name，暫時使用第一台「%s」；正式派遣前必須填寫", matched[0].get("name"))
        self._robot_id = int(matched[0]["id"])
        return self._robot_id

    def route_id(self, location: str) -> int:
        if location in self._route_ids:
            return self._route_ids[location]
        robot_id = self.robot_id()
        target = self._config.routes.get(location)
        if target is None:
            raise RimError(f"設定檔的 routes 沒有地點「{location}」")
        if isinstance(target, int) or str(target).isdigit():
            route_id = int(target)
        else:
            routes = self._client.eroutes(robot_id)
            matched = [route for route in routes if route.get("name") == target]
            if not matched:
                available = "、".join(str(route.get("name")) for route in routes) or "（無）"
                raise RimError(f"找不到名稱為「{target}」的 eRoute，目前有：{available}")
            route_id = int(matched[0]["id"])
        self._route_ids[location] = route_id
        return route_id


class ConfiguredPlanner:
    """實作 incident.ObservationPlanner。沒有設定跌倒區域時，固定前往設定檔的預設地點。"""

    def __init__(self, location: str):
        self._location = location

    def choose(self, event) -> str:
        return self._location


def main() -> None:
    parser = argparse.ArgumentParser(description="測試 RIM2 連線與派遣 Temi")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Temi 設定檔路徑")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--list", action="store_true", help="列出機器人與 eRoute，不會移動 Temi")
    group.add_argument("--check", metavar="地點", help="確認地點對應的 eRoute 存在，不會移動 Temi")
    group.add_argument("--go", metavar="地點", help="派遣 Temi 前往設定檔中的地點（Temi 會移動）")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        config = load_config(args.config)
        client = RimClient(config)
        controller = RimRobotController(client, config, require_robot_name=bool(args.go))
        robot_id = controller.robot_id()
        if args.list:
            for robot in client.robots():
                mark = "（使用中）" if int(robot["id"]) == robot_id else ""
                print(f"機器人 {robot['id']}：{robot.get('name')}{mark}")
            for route in client.eroutes(robot_id):
                print(f"  eRoute {route['id']}：{route.get('name')}")
        elif args.check:
            print(f"地點「{args.check}」對應 eRoute {controller.route_id(args.check)}，設定正確（未派遣）")
        else:
            controller.dispatch(args.go)
            print("已送出派遣任務")
    except RimError as error:
        logger.error("%s", error)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

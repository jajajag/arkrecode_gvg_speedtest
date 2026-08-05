import base64
import hashlib
import json
import random
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import requests


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / 'data'
ACCOUNT_PATH = DATA_DIR / 'account.json'
MAIN_ACCOUNT = 'MainAccount'
SUB_ACCOUNT = 'SubAccount'

GAME_URL = 'https://game-arkre-labs.ecchi.xxx/Router/RouterHandler.ashx'
TOKEN_URL = 'https://sadpki-portal-v2.ebuajk.com/api/v2/token/access'
GAME_HEADERS = {
    'Content-Type': 'application/octet-stream',
    'User-Agent': (
        'UnityPlayer/2022.3.62f2 '
        '(UnityWebRequest/1.0, libcurl/8.10.1-DEV)'
    ),
}

requests.packages.urllib3.disable_warnings()

MAX_INFO_IMAGES = 3
MAX_INFO_IMAGE_BYTES = 8 * 1024 * 1024
INFO_IMAGE_LOCK = threading.RLock()


class ConfigError(RuntimeError):
    pass


class GameRequestError(RuntimeError):
    pass


def oid(value):
    if isinstance(value, dict):
        return str(value.get('$oid') or value.get('$id') or '')
    return str(value or '')


def _image_extension(data):
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return '.webp'
    if data.startswith(b'BM'):
        return '.bmp'
    return None


def cache_info_images(image_data, images_dir):
    image_data = list(image_data)
    if len(image_data) > MAX_INFO_IMAGES:
        raise GameRequestError(
            '每次最多上传 {} 张图片'.format(MAX_INFO_IMAGES))
    prepared = []
    with requests.Session() as http:
        for item in image_data:
            url = str(item.get('url') or item.get('file') or '').strip()
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                raise GameRequestError('图片消息缺少可下载的 HTTP 地址')
            with http.get(url, stream=True, timeout=60) as response:
                response.raise_for_status()
                content_length = response.headers.get('Content-Length')
                if content_length and int(content_length) > MAX_INFO_IMAGE_BYTES:
                    raise GameRequestError('单张图片不能超过 8MB')
                chunks = []
                size = 0
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > MAX_INFO_IMAGE_BYTES:
                        raise GameRequestError('单张图片不能超过 8MB')
                    chunks.append(chunk)
            data = b''.join(chunks)
            extension = _image_extension(data)
            if not extension:
                raise GameRequestError('只支持 JPG、PNG、GIF、WEBP 或 BMP 图片')
            digest = hashlib.sha256(data).hexdigest()
            prepared.append((digest + extension, data))

    images_dir = Path(images_dir)
    relative_paths = []
    with INFO_IMAGE_LOCK:
        images_dir.mkdir(parents=True, exist_ok=True)
        for filename, data in prepared:
            destination = images_dir / filename
            if not destination.exists():
                temp_path = images_dir / (filename + '.tmp')
                temp_path.write_bytes(data)
                temp_path.replace(destination)
            relative_paths.append('images/{}'.format(filename))
    return relative_paths


_ACCOUNT_FILE_LOCK = threading.Lock()


def _read_account_file(path):
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            '找不到 {}，请复制 account_example.json 后填写。'.format(path.name))
    try:
        with path.open('r', encoding='utf-8') as file:
            config = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError('account.json 读取失败：{}'.format(exc)) from exc
    if not isinstance(config, dict):
        raise ConfigError('account.json 顶层必须是 JSON 对象')
    return config


def load_config(account, path=ACCOUNT_PATH):
    with _ACCOUNT_FILE_LOCK:
        config = _read_account_file(path)
    account_config = config.get(account)
    if not isinstance(account_config, dict):
        raise ConfigError('account.json 缺少账号节点：{}'.format(account))
    required = ('Name', 'Token')
    missing = [key for key in required
               if not str(account_config.get(key, '')).strip()]
    if missing:
        raise ConfigError('account.json 的 {} 缺少：{}'.format(
            account, '、'.join(missing)))
    return dict(account_config)


def save_config(account, account_config, path=ACCOUNT_PATH):
    path = Path(path)
    with _ACCOUNT_FILE_LOCK:
        config = _read_account_file(path)
        config[account] = dict(account_config)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(path.suffix + '.tmp')
        with temp_path.open('w', encoding='utf-8') as file:
            json.dump(config, file, ensure_ascii=False, indent=2)
        temp_path.replace(path)


class GameClient:
    def __init__(self, config, account=SUB_ACCOUNT,
                 account_path=ACCOUNT_PATH, session=None):
        self.config = config
        self.account = account
        self.account_path = Path(account_path)
        self.http = session or requests.Session()
        self.aid = None
        self.session_id = None
        self.cuid = None
        self.bulletin = None
        self.rank_week = None
        self._request_lock = threading.RLock()

    def _post(self, url, payload=None, headers=None, timeout=60):
        response = self.http.post(
            url,
            json=payload,
            headers=headers or GAME_HEADERS,
            verify=False,
            timeout=timeout,
        )
        response.raise_for_status()
        response.encoding = 'utf-8'
        data = response.json()
        self._check_response(data)
        return data

    @staticmethod
    def _check_response(data):
        if not isinstance(data, dict):
            raise GameRequestError('服务器返回的不是 JSON 对象')
        error = data.get('Error') or data.get('error')
        error_code = data.get('ErrorCode')
        code = data.get('Code', data.get('code'))
        bad_code = code not in (None, 0, '0', 200, '200', 'Success', 'success')
        if data.get('Success') is False or error \
                or error_code not in (None, 0, '0', '') or bad_code:
            message = data.get('Message') or data.get('message')
            raise GameRequestError(str(message or error or error_code or code))

    def _send_route(self, route, data, delay=None):
        if delay:
            time.sleep(random.uniform(*delay))
        return self._post(GAME_URL, {'route': route, 'data': data})

    def _login_once(self):
        bulletin = query_bulletin(self.http)
        info = bulletin.get('Info') or {}
        versions = info.get('AvailableVersions') or []
        if not versions:
            raise GameRequestError('公告响应缺少 AvailableVersions')

        token = self.config['Token']
        parts = token.split('.')
        if len(parts) < 2:
            raise ConfigError('Token 不是有效的 JWT')
        encoded = parts[1] + '=' * (-len(parts[1]) % 4)
        try:
            token_data = json.loads(base64.urlsafe_b64decode(encoded))
            login_id = token_data['user_id']
        except Exception as exc:
            raise ConfigError('Token 无法解析 user_id') from exc

        is_new_sdk = 'exp' in token_data
        login_token = token
        if is_new_sdk:
            headers = dict(GAME_HEADERS)
            headers['Authorization'] = 'Bearer {}'.format(token)
            token_result = self._post(
                TOKEN_URL, headers=headers, timeout=60)
            token_payload = token_result.get('data') or {}
            login_id = token_payload.get('userId')
            login_token = token_payload.get('accessToken')
            refresh_token = token_payload.get('refreshToken')
            if not login_id or not login_token or not refresh_token:
                raise GameRequestError('刷新 Token 的响应字段不完整')
            self.config['Token'] = refresh_token
            save_config(self.account, self.config, self.account_path)

        result = self._send_route('AccountHandler.Login', {
            'LoginID': login_id,
            'Token': login_token,
            'Version': versions[-1],
            'LoginType': 'Erolabs',
            'IsNewSDK': is_new_sdk,
        })
        account_info = result.get('Info') or {}
        self.aid = oid(account_info.get('_id'))
        self.session_id = result.get('SessionID')
        self.cuid = account_info.get('CUID')
        if not self.aid or not self.session_id or self.cuid is None:
            raise GameRequestError('登录响应缺少 AID、SessionID 或 CUID')
        self.bulletin = bulletin
        self.rank_week = (((result.get('PVPData') or {}).get(
            'PVPRankInfo') or {}).get('RankWeek'))
        return result

    def login(self, attempts=3, force=False):
        with self._request_lock:
            if self.session_id and not force:
                return None
            last_error = None
            for attempt in range(1, attempts + 1):
                try:
                    return self._login_once()
                except Exception as exc:
                    last_error = exc
                    self.aid = self.session_id = self.cuid = None
                    self.rank_week = None
                    if attempt < attempts:
                        time.sleep(attempt)
            raise GameRequestError('连续登录 {} 次失败：{}'.format(
                attempts, last_error)) from last_error

    def call(self, route, data=None, attempts=3, delay=None,
             required_key=None):
        with self._request_lock:
            last_error = None
            for attempt in range(1, attempts + 1):
                try:
                    if not self.session_id:
                        self.login(attempts=3)
                    payload = dict(data or {})
                    payload.update({
                        'AID': self.aid,
                        'SessionID': self.session_id,
                    })
                    result = self._send_route(route, payload, delay=delay)
                    if required_key and required_key not in result:
                        raise GameRequestError(
                            '{} 响应缺少 {}'.format(route, required_key))
                    return result
                except Exception as exc:
                    last_error = exc
                    self.aid = self.session_id = self.cuid = None
                    self.rank_week = None
                    if attempt < attempts:
                        self.login(attempts=3)
            raise GameRequestError(
                '{} 连续请求 {} 次失败：{}'.format(
                    route, attempts, last_error)
            ) from last_error

    def call_once(self, route, data=None, delay=None, required_key=None):
        """Send one request without clearing or refreshing the session."""
        with self._request_lock:
            if not self.session_id:
                raise GameRequestError('当前没有有效登录会话')
            payload = dict(data or {})
            payload.update({'AID': self.aid, 'SessionID': self.session_id})
            result = self._send_route(route, payload, delay=delay)
            if required_key and required_key not in result:
                raise GameRequestError(
                    '{} 响应缺少 {}'.format(route, required_key))
            return result


def query_bulletin(session=None):
    """Fetch public patch/version metadata without a game login."""
    owns_session = session is None
    http = session or requests.Session()
    try:
        response = http.post(
            GAME_URL,
            json={
                'route': (
                    'GameServerDBSettingHandler.QueryBulletinInfoResult'),
                'data': {},
            },
            headers=GAME_HEADERS,
            verify=False,
            timeout=60,
        )
        response.raise_for_status()
        response.encoding = 'utf-8'
        data = response.json()
        GameClient._check_response(data)
        return data
    finally:
        if owns_session:
            http.close()


_SHARED_CLIENTS = {}
_SHARED_CLIENT_LOCK = threading.Lock()


def get_shared_game_client(account):
    """Return the process-wide client for one configured game account."""
    if account not in (MAIN_ACCOUNT, SUB_ACCOUNT):
        raise ConfigError('未知账号节点：{}'.format(account))
    with _SHARED_CLIENT_LOCK:
        if account not in _SHARED_CLIENTS:
            _SHARED_CLIENTS[account] = GameClient(
                load_config(account), account=account)
        return _SHARED_CLIENTS[account]


def get_main_game_client():
    return get_shared_game_client(MAIN_ACCOUNT)


def get_sub_game_client():
    return get_shared_game_client(SUB_ACCOUNT)


def query_guild(client, guild_id):
    return client.call(
        'GuildHandler.QueryPartialGuildDataForGuildWar',
        {'GuildID': guild_id},
        delay=(0.8, 1.2),
        required_key='GuildData',
    )


def query_full_guild_war_data(client):
    return client.call(
        'GuildWarHandler.QueryFullGuildWarData',
        {},
        delay=(0.8, 1.2),
        required_key='GuildWarData',
    )


def query_pvp_rank(client):
    if client.rank_week is None:
        raise GameRequestError('登录响应缺少 PVP RankWeek')
    return client.call(
        'PVPHandler.GetPVPRankList',
        {'Week': client.rank_week},
        delay=(0.8, 1.2),
        required_key='PVPRankInfoList',
    )


def query_top_guilds(client):
    rank_data = client.call(
        'GuildWarHandler.QueryNowGuildWarRank',
        {},
        delay=(0.8, 1.2),
        required_key='GuildWarCampaignInfoList',
    )
    ranked = rank_data.get('GuildWarCampaignInfoList') or []
    guilds = []
    seen_ids = set()
    for item in ranked:
        guild_id = oid((item.get('GuildSubInfo') or {}).get('_id'))
        if not guild_id or guild_id in seen_ids:
            continue
        seen_ids.add(guild_id)
        guilds.append(query_guild(client, guild_id))
    if not guilds:
        raise GameRequestError('团战排行榜前20名中没有可查询的佣兵团')
    return guilds


def search_friend_players(client, query):
    data = client.call(
        'FriendHandler.SearchFriendList',
        {'Name': query},
        delay=(0.8, 1.2),
    )
    players = []
    seen = set()
    for player in data.get('FriendInfos') or []:
        cuid = player.get('CUID')
        if cuid is None or int(player.get('LV') or 0) <= 60:
            continue
        cuid = int(cuid)
        if cuid in seen:
            continue
        seen.add(cuid)
        players.append(player)
    return players


def query_player_card(client, cuid):
    return client.call(
        'AccountHandler.QueryPlayerCardData',
        {'CUID': int(cuid)},
        delay=(0.8, 1.2),
    )


def query_member_logs(client, cuid):
    return client.call(
        'GuildWarHandler.QueryGuildWarBattleLogListByAccount',
        {'TargetCUID': int(cuid)},
        delay=(0.08, 0.12),
        required_key='SubLogs',
    )


def query_battle_detail(client, battle_id):
    return client.call_once(
        'GuildWarHandler.QueryGuildWarBattleLogByID',
        {'TargetCUID': int(client.cuid), 'TargetID': battle_id},
        delay=(0.8, 1.2),
        required_key='Logs',
    )

import json
from pathlib import Path
from .store import ClubError


def load_config(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('guilds'), dict):
        raise ClubError('Настройка должна содержать объект guilds с ID серверов.')
    result = {}
    for guild, settings in data['guilds'].items():
        if not guild.isdecimal() or int(guild) <= 0 or not isinstance(settings, dict):
            raise ClubError('Некорректный ID сервера или настройки.')
        result[int(guild)] = settings
    return result

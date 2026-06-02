# mcp-server-matrix

[Model Context Protocol (MCP)](https://modelcontextprotocol.io) сервер для [Matrix](https://matrix.org) — открытого децентрализованного протокола общения.

Построен на [matrix-nio](https://github.com/matrix-nio/matrix-nio). Позволяет любому MCP-совместимому AI-ассистенту (Claude, OpenClaw и др.) отправлять и читать сообщения, управлять комнатами и взаимодействовать с любым Matrix-сервером (Synapse, Dendrite, Conduit) — как публичным (matrix.org), так и self-hosted.

## Возможности

| Инструмент | Описание |
|------------|----------|
| `send_message` | Отправить текстовое сообщение в комнату |
| `send_html` | Отправить сообщение с HTML-форматированием |
| `read_messages` | Прочитать последние сообщения (с пагинацией) |
| `list_rooms` | Список всех комнат, в которых состоит пользователь |
| `get_room_info` | Информация о комнате — название, тема, участники, шифрование |
| `get_room_members` | Список участников комнаты с именами и аватарами |
| `join_room` | Войти в комнату по ID или алиасу |
| `leave_room` | Покинуть комнату |
| `create_room` | Создать новую комнату (с приглашениями) |
| `invite_user` | Пригласить пользователя в комнату |
| `send_reaction` | Поставить реакцию (эмодзи) на сообщение |
| `resolve_alias` | Разрешить алиас `#комната:сервер` в ID комнаты |

## Установка

```bash
pip install mcp-server-matrix
```

Или через [uv](https://docs.astral.sh/uv/):

```bash
uvx mcp-server-matrix
```

## Настройка

Переменные окружения:

| Переменная | Обязательна | Описание |
|------------|-------------|----------|
| `MATRIX_HOMESERVER` | Да | URL сервера (напр. `https://matrix.org` или `https://matrix.example.com`) |
| `MATRIX_USER` | Да | Полный ID пользователя (напр. `@bot:matrix.org`) |
| `MATRIX_PASSWORD` | Да* | Пароль аккаунта |
| `MATRIX_ACCESS_TOKEN` | Да* | Access token (альтернатива паролю) |
| `MATRIX_STORE_PATH` | Нет | Путь для хранения сессии (по умолчанию: `~/.mcp-server-matrix/nio_store/`) |
| `MCP_LOG_LEVEL` | Нет | Уровень логирования: DEBUG, INFO, WARNING, ERROR (по умолчанию: INFO) |

\* Укажите либо `MATRIX_PASSWORD`, либо `MATRIX_ACCESS_TOKEN`.

## Использование

### Claude Desktop

Добавьте в `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "matrix": {
      "command": "mcp-server-matrix",
      "env": {
        "MATRIX_HOMESERVER": "https://matrix.example.com",
        "MATRIX_USER": "@bot:example.com",
        "MATRIX_PASSWORD": "your-password"
      }
    }
  }
}
```

### Claude Code

```json
{
  "mcpServers": {
    "matrix": {
      "command": "mcp-server-matrix",
      "env": {
        "MATRIX_HOMESERVER": "https://matrix.example.com",
        "MATRIX_USER": "@bot:example.com",
        "MATRIX_ACCESS_TOKEN": "syt_..."
      }
    }
  }
}
```

### OpenClaw

Добавьте в `openclaw.json`:

```json
{
  "mcp": {
    "servers": {
      "matrix": {
        "command": "mcp-server-matrix",
        "env": {
          "MATRIX_HOMESERVER": "https://matrix.example.com",
          "MATRIX_USER": "@bot:example.com",
          "MATRIX_PASSWORD": "your-password"
        }
      }
    }
  }
}
```

### Напрямую (stdio)

```bash
export MATRIX_HOMESERVER=https://matrix.example.com
export MATRIX_USER=@bot:example.com
export MATRIX_PASSWORD=your-password

mcp-server-matrix
```

## Примеры

После подключения AI-ассистент сможет:

- **Читать сообщения**: "Что нового в #general?"
- **Отправлять сообщения**: "Отправь 'Всем привет!' в #announcements"
- **Управлять комнатами**: "Создай комнату 'Проект X' и пригласи @alice:matrix.org"
- **Получать информацию**: "Сколько участников в #team-chat?"

## Поддерживаемые серверы

Работает с любым Matrix-сервером, поддерживающим Client-Server API:

- [Synapse](https://github.com/element-hq/synapse) — эталонная реализация
- [Dendrite](https://github.com/matrix-org/dendrite) — легковесный сервер на Go
- [Conduit](https://conduit.rs) — быстрый сервер на Rust
- [matrix.org](https://matrix.org) — публичный сервер

## Требования

- Python 3.10+
- Аккаунт на любом Matrix-сервере
- Сетевой доступ к серверу

## Лицензия

MIT

## Ссылки

- [Model Context Protocol](https://modelcontextprotocol.io)
- [matrix-nio](https://github.com/matrix-nio/matrix-nio)
- [Спецификация Matrix](https://spec.matrix.org)

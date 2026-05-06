# 🍪 Cookies для скачивания медиа

## Какие платформы требуют cookies?

| Платформа | Cookies | Статус |
|-----------|---------|--------|
| **Instagram** | ✅ Обязательны | Без cookies скачивание не работает |
| **YouTube** | ⚠️ Рекомендуются | Для возрастного контента и подписок |
| **TikTok** | ❌ Не нужны | Работает без cookies |
| **Pinterest** | ❌ Не нужны | Работает без cookies |
| **VK** | ❌ Не нужны | Работает без cookies |

## Зачем нужны cookies?

Instagram требует авторизации для доступа к контенту. Без cookies yt-dlp получит ошибку:
- "Login required"
- "This content is not available"
- "403 Forbidden"

## Как получить cookies?

### Способ 1: Автоматический (рекомендуется)

```bash
# Извлечь cookies из Chrome для Instagram
uv run scripts/extract_cookies.py instagram

# Извлечь cookies из Firefox
uv run scripts/extract_cookies.py instagram -b firefox

# Извлечь для всех платформ сразу
uv run scripts/extract_cookies.py --all
```

**Требования:**
- Вы должны быть **залогинены** в Instagram в браузере
- Браузер должен быть закрыт (для Chrome) или можно использовать (для Firefox)

### Способ 2: Вручную через расширение браузера

1. Установите расширение **"Get cookies.txt LOCALLY"** или **"EditThisCookie"**
2. Зайдите на Instagram.com и убедитесь что вы авторизованы
3. Откройте расширение и экспортируйте cookies
4. Сохраните в формате **Netscape** как `storage/cookies/instagram_cookies.txt`

### Способ 3: Вручную через Developer Tools

1. Откройте Instagram.com в браузере
2. Нажмите F12 → Application → Cookies
3. Найдите ключевые cookies:
   - `sessionid` (обязательно)
   - `ds_user_id` (обязательно)
   - `csrftoken`
   - `mid`
   - `ig_did`
4. Сохраните в Netscape формате

## Где хранятся cookies?

```
storage/cookies/
├── instagram_cookies.txt  # Instagram cookies
├── youtube_cookies.txt    # YouTube cookies (опционально)
└── ...
```

## Нужно ли обновлять cookies?

**Да!** Cookies имеют срок действия:

| Платформа | Срок действия | Когда обновлять |
|-----------|---------------|-----------------|
| Instagram | 30-90 дней | Когда скачивание перестало работать |
| YouTube | 6-12 месяцев | По необходимости |

### Признаки что cookies устарели:

- ❌ Ошибка "Login required"
- ❌ Ошибка "403 Forbidden"
- ❌ Скачивание выдает пустые файлы
- ❌ В логах: "cookies" или "authentication" ошибки

### Как обновить:

```bash
# Просто запустите скрипт снова - он перезапишет файл
uv run scripts/extract_cookies.py instagram
```

## Автоматическое обновление cookies

В будущем можно добавить:
1. **Cron job** - напоминание об обновлении
2. **Admin панель** - статус cookies и кнопка обновления
3. **Email уведомления** - когда cookies истекают

## Troubleshooting

### "No cookies found"
- Убедитесь что вы залогинены в Instagram в браузере
- Закройте браузер и попробуйте снова (для Chrome)
- Попробуйте другой браузер

### "Invalid cookie format"
- Убедитесь что файл в **Netscape формате**
- Используйте скрипт `extract_cookies.py` для правильного формата

### Cookies работают но скачивание не работает
- Проверьте что аккаунт Instagram не заблокирован
- Попробуйте другой аккаунт
- Обновите cookies (они могли истечь)

## Безопасность

⚠️ **Важно:**
- Cookies файлы содержат чувствительные данные
- Не коммитьте их в Git! (уже в `.gitignore`)
- Не передавайте третьим лицам
- При компрометации - выйдите из аккаунта на всех устройствах

## Добавление поддержки для других платформ

Если нужно добавить cookies для новой платформы:

1. Добавьте платформу в `scripts/extract_cookies.py`:
```python
platform_domains = {
    "new_platform": [".new-platform.com"],
}
```

2. Обновите downloader чтобы использовал cookies:
```python
cookies_path = COOKIES_DIR / f"{platform}_cookies.txt"
if cookies_path.exists():
    opts['cookiefile'] = str(cookies_path)
```

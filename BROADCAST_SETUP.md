# 🔴 ВАЖНО: Как запустить Broadcast рассылку

## Проблема

Broadcast (массовая рассылка) **НЕ РАБОТАЕТ** потому что **ARQ Worker не запущен!**

## Почему так?

MediaFlow использует **два отдельных процесса**:

1. **Веб-сервер** (`python main.py server`) - обрабатывает HTTP запросы и админку
2. **ARQ Worker** (`python main.py worker`) - выполняет фоновые задачи (broadcast рассылку)

Когда вы создаете broadcast и нажимаете "Отправить", задача ставится в очередь Redis. 
**Только ARQ worker может взять эту задачу и выполнить!**

Без worker'а задача остается в очереди навсегда и никогда не выполняется.

---

## ✅ Как правильно запустить

### Способ 1: Два терминала (рекомендуется)

**Терминал 1 - Веб-сервер:**
```bash
cd /home/sayavdera/Desktop/projects/TelegramBots/MediaFlow
python main.py server
```

**Терминал 2 - ARQ Worker:**
```bash
cd /home/sayavdera/Desktop/projects/TelegramBots/MediaFlow  
python main.py worker
```

### Способ 2: Один скрипт (автоматически)

```bash
chmod +x start_both.sh
./start_both.sh
```

Это запустит оба процесса автоматически.

---

## 🔍 Как проверить что всё работает

### Проверить процессы:
```bash
ps aux | grep "main.py" | grep -v grep
```

Должно быть **два процесса**:
- Один с `server` или `granian`
- Второй с `worker` или `arq`

### Проверить логи worker'а:
```bash
# Ищите сообщение о запуске
grep "Worker starting\|Worker ready" storage/logs/*.log

# Или смотрите все логи worker
tail -f storage/logs/*.log | grep -i worker
```

### Проверить Redis очередь:
```bash
redis-cli
> LRANGE arq:queue:mediadownloader 0 -1
```

Если видите задачи в очереди - значит worker их должен обрабатывать!

---

## 📊 Пошаговая проверка Broadcast

1. **Запустите оба процесса:**
   ```bash
   # Терминал 1
   python main.py server &
   
   # Терминал 2  
   python main.py worker &
   ```

2. **Создайте broadcast:**
   - Откройте `/admin/ads/create`
   - Выберите тип: **Broadcast**
   - Введите название и текст
   - **Важно:** Выберите хотя бы одного бота!
   - Сохраните

3. **Запустите рассылку:**
   - Откройте созданную рекламу
   - Нажмите "Send Broadcast"
   - Статус должен измениться на "QUEUED" → "SENDING" → "COMPLETED"

4. **Проверьте логи:**
   ```bash
   # В логах worker'а должно быть:
   grep "broadcast" storage/logs/*.log | tail -20
   ```

   Ожидаемые сообщения:
   - `"Starting broadcast"` - worker взял задачу
   - `"Broadcast ad loaded"` - загрузил рекламу
   - `"Broadcasting to users"` - начал рассылку
   - `"Broadcast completed"` - закончил с статистикой

5. **Проверьте базу:**
   ```sql
   SELECT id, name, status, sent_count, failed_count 
   FROM ads 
   ORDER BY created_at DESC 
   LIMIT 5;
   ```

---

## ❌ Распространенные ошибки

### "queue_failed" после нажатия Send
**Причина:** ARQ worker не запущен или Redis не доступен

**Решение:**
```bash
# Проверить Redis
redis-cli ping  # Должно вернуть PONG

# Запустить worker
python main.py worker
```

### Broadcast застрял в "QUEUED"
**Причина:** Worker не запущен или не может подключиться к Redis

**Решение:**
```bash
# Проверить логи worker
tail -f storage/logs/*.log

# Перезапустить worker
pkill -f "main.py worker"
python main.py worker
```

### Broadcast застрял в "SENDING"
**Причина:** Worker crashed mid-broadcast

**Решение:**
```sql
-- Сбросить статус чтобы можно было запустить снова
UPDATE ads 
SET status = 'DRAFT', started_at = NULL
WHERE status = 'SENDING';
```

### "No target bots selected"
**Причина:** При создании broadcast не выбраны боты

**Решение:** Отредактируйте рекламу и выберите хотя бы одного бота

---

## 🔧 Автоматический запуск при загрузке (systemd)

Если хотите чтобы оба процесса запускались автоматически:

```bash
# Скопируйте service файлы
sudo cp scripts/systemd/mediaflow.service /etc/systemd/system/
sudo cp scripts/systemd/mediaflow-worker.service /etc/systemd/system/

# Включите автозапуск
sudo systemctl enable mediaflow
sudo systemctl enable mediaflow-worker

# Запустите
sudo systemctl start mediaflow
sudo systemctl start mediaflow-worker

# Проверьте статус
sudo systemctl status mediaflow mediaflow-worker
```

---

## 📞 Если всё ещё не работает

1. **Проверьте оба процесса:**
   ```bash
   ps aux | grep "main.py" | grep -v grep
   ```

2. **Проверьте Redis:**
   ```bash
   redis-cli ping
   ```

3. **Проверьте базу:**
   ```bash
   sqlite3 database.db "SELECT * FROM ads ORDER BY created_at DESC LIMIT 3;"
   ```

4. **Посмотрите логи:**
   ```bash
   tail -100 storage/logs/*.log | grep -i "error\|exception\|broadcast"
   ```

5. **Перезапустите оба процесса:**
   ```bash
   pkill -f "main.py"
   python main.py server &
   python main.py worker &
   ```

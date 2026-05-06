#!/bin/bash
# Запуск MediaFlow с обоими процессами

echo "🚀 Starting MediaFlow..."
echo ""
echo "📡 Terminal 1: Web Server"
echo "📮 Terminal 2: ARQ Worker (for broadcast)"
echo ""

# Запускаем веб-сервер в фоне
python main.py server &
SERVER_PID=$!
echo "✅ Web server started (PID: $SERVER_PID)"

# Запускаем ARQ worker в фоне
python main.py worker &
WORKER_PID=$!
echo "✅ ARQ worker started (PID: $WORKER_PID)"

echo ""
echo "🎉 Both processes running!"
echo "   Web server: http://127.0.0.1:8000"
echo "   Admin panel: http://127.0.0.1:8000/admin"
echo ""
echo "📊 To stop: kill $SERVER_PID and $WORKER_PID"
echo "   Or run: pkill -f 'main.py'"
echo ""

# Ждем завершения
wait

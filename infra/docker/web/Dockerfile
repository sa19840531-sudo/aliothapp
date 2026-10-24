FROM node:22-bookworm-slim

WORKDIR /workspace

ENV CHOKIDAR_USEPOLLING=true

EXPOSE 5173

CMD ["sh", "-c", "if [ -f package.json ]; then npm install && npm run dev -- --host 0.0.0.0 --port 5173; else echo '前端项目尚未初始化。'; tail -f /dev/null; fi"]

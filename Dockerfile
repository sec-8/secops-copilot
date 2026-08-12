FROM python:3.12-slim 
# 系统依赖：healthcheck 用 curl，uvicorn --reload 需要 watchfiles 在 PyPI，libgomp1 按需保留
RUN apt-get update && \
    apt-get install -y --no-install-recommends curl libgomp1 && \
    rm -rf /var/lib/apt/list/*
# 创建非 root 用户
RUN useradd -m -u 1000 appuser

WORKDIR /app
# 复制依赖文件（利用 Docker 缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码
COPY . .

# 调整权限
RUN chown -R appuser:appuser /app
USER appuser
# 暴露端口
EXPOSE 8000
# 健康检查（确保项目有 /health 端点，否则改掉）
HEALTHCHECK --interval=30s --timeout=3s \
  CMD curl -f http://localhost:8000/health || exit 1

# 启动命令（开发模式 --reload，需 watchfiles）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
# fastapi 版本 CMD ["fastapi", "run", "app/main.py", "--host", "0.0.0.0", "--port", "8000", "--reload"]
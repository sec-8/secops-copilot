-- ================================================
-- SecOps Copilot - PostgreSQL 初始化脚本
-- ================================================
-- 数据库：secops_copilot
-- 表：user_facts（长期 Memory 事实存储）
-- 用法：
--   psql -U postgres -f scripts/init_db.sql
-- 或 在 Docker 内：
--   docker exec -i secops-postgres psql -U postgres < scripts/init_db.sql
-- ================================================

-- 1. 创建数据库（如果不存在）
SELECT 'CREATE DATABASE secops_copilot'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'secops_copilot')\gexec

-- 切到 secops_copilot 库
\c secops_copilot

-- 2. 启用 uuid 扩展（v2 用来给每条 fact 一个 uuid）
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 3. user_facts 表（长期 Memory 主表）
CREATE TABLE IF NOT EXISTS user_facts (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id     TEXT NOT NULL,                  -- 用户 ID（前端透传 / anonymous）
    key         TEXT NOT NULL,                  -- 事实 key（user_name / job / ...）
    value       TEXT NOT NULL,                  -- 事实 value
    confidence  REAL DEFAULT 1.0,               -- LLM 抽取置信度 [0, 1]
    created_at  TIMESTAMPTZ DEFAULT NOW(),      -- 创建时间
    updated_at  TIMESTAMPTZ DEFAULT NOW(),      -- 更新时间

    -- 约束：同一 user 同一 key 唯一（覆盖式更新）
    CONSTRAINT user_facts_user_key_unique UNIQUE (user_id, key)
);

-- 4. 索引（按 user_id 查 / 按 created_at 排序）
CREATE INDEX IF NOT EXISTS idx_user_facts_user_id
    ON user_facts (user_id);

CREATE INDEX IF NOT EXISTS idx_user_facts_created_at
    ON user_facts (created_at DESC);

-- 5. updated_at 自动更新触发器
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_facts_updated_at ON user_facts;
CREATE TRIGGER trg_user_facts_updated_at
    BEFORE UPDATE ON user_facts
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- 6. 验证
SELECT 'user_facts 表已就绪' AS status;
SELECT COUNT(*) AS existing_facts FROM user_facts;

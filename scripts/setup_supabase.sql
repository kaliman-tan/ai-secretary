-- ユーザープロフィール
CREATE TABLE IF NOT EXISTS user_profile (
    id BIGSERIAL PRIMARY KEY,
    name TEXT,
    role TEXT,
    priorities TEXT,
    timezone TEXT DEFAULT 'Asia/Tokyo',
    preferences JSONB DEFAULT '{}',
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- カスタムルール
CREATE TABLE IF NOT EXISTS custom_rules (
    id BIGSERIAL PRIMARY KEY,
    rule_type TEXT NOT NULL,   -- 'sender' | 'subject' | 'keyword'
    target TEXT NOT NULL,
    action TEXT NOT NULL,      -- '重要' | '無視' | 'タスク追加' など
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 重要連絡先
CREATE TABLE IF NOT EXISTS contacts (
    id BIGSERIAL PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    notes TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- ToDoリスト
CREATE TABLE IF NOT EXISTS todos (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    priority TEXT DEFAULT 'normal',  -- 'high' | 'normal' | 'low'
    done BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- 判断履歴
CREATE TABLE IF NOT EXISTS decision_history (
    id BIGSERIAL PRIMARY KEY,
    context TEXT NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- メールログ（重複通知防止 + 日次レポート用）
CREATE TABLE IF NOT EXISTS email_logs (
    id BIGSERIAL PRIMARY KEY,
    message_id TEXT UNIQUE NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    importance TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 就活リサーチログ（重複通知防止）
CREATE TABLE IF NOT EXISTS research_logs (
    id BIGSERIAL PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    summary TEXT DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- インデックス
CREATE INDEX IF NOT EXISTS idx_todos_done ON todos(done);
CREATE INDEX IF NOT EXISTS idx_email_logs_message_id ON email_logs(message_id);
CREATE INDEX IF NOT EXISTS idx_custom_rules_type ON custom_rules(rule_type);
CREATE INDEX IF NOT EXISTS idx_research_logs_url ON research_logs(url);

-- user_profile カラム追加（初回 setup_supabase.sql 実行後に不足が判明したカラム）
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS hobbies     TEXT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS traits      TEXT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS notes       TEXT;
ALTER TABLE user_profile ADD COLUMN IF NOT EXISTS career_goal TEXT;

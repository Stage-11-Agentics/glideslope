CREATE TABLE logs (
    thread_id TEXT,
    ts INTEGER,
    ts_nanos INTEGER,
    feedback_log_body TEXT,
    target TEXT
);
INSERT INTO logs VALUES ('thread-main', 0, 800000000, '{"service_tier":"default"}', 'feedback_tags');
INSERT INTO logs VALUES ('thread-main', 1, 500000000, '{"service_tier":"priority"}', 'feedback_tags');
INSERT INTO logs VALUES ('thread-main', 3, 500000000, '{"service_tier":"default"}', 'feedback_tags');
INSERT INTO logs VALUES ('thread-main', 2, 0, '{"service_tier":"ultrafast"}', 'other');

-- Runs on every node at first start. Not written to the binlog: each node creates its own
-- copy, and only later application writes replicate from the primary.
SET SESSION sql_log_bin = 0;

-- Accounts ProxySQL needs on the backends
CREATE USER IF NOT EXISTS 'monitor'@'%' IDENTIFIED BY 'monitor';
GRANT USAGE, REPLICATION CLIENT, SLAVE MONITOR ON *.* TO 'monitor'@'%';

CREATE USER IF NOT EXISTS 'app'@'%' IDENTIFIED BY 'apppass';
CREATE DATABASE IF NOT EXISTS appdb;
GRANT ALL ON appdb.* TO 'app'@'%';

CREATE TABLE IF NOT EXISTS appdb.orders (
  id INT AUTO_INCREMENT PRIMARY KEY,
  customer VARCHAR(64) NOT NULL,
  amount DECIMAL(10,2) NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

SET SESSION sql_log_bin = 1;

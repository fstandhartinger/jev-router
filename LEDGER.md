# Provisioning ledger

- 21 September 2026: created Neon project `jev-router-production` in Frankfurt (`aws-eu-central-1`) for durable account, key, credit, webhook, and usage records. Connection credentials are stored outside git in `/home/flori/.config/jev-router.env`. No paid compute was enabled.
- 21 September 2026: no GPU workers were started. SemIf, djev, decider, and Laya endpoint environment variables remain unset until their scale-to-zero services are validated.

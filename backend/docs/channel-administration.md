# Channel administration contract

Enterprise channel administration may use `X-Zhiyuan-Channel-Admin` only with
a validated internal identity. The HMAC binds the method, channel path and raw
owner header; normal internal chat calls remain non-admin. Portal callers must
revalidate the human administrator before signing and strip client-supplied
capability headers from ordinary chat proxy requests.

For runtime credential updates, an omitted password field preserves an existing
secret; explicit empty input fails required validation. The existing masked
`********` update remains supported. Neither response returns raw credentials.
WeCom connectivity requires SDK authentication plus an open socket; WeChat
requires a recent successful platform poll. Worker startup alone is insufficient.

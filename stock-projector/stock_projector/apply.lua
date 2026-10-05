-- Atomic compare-and-set on revision (contract section 4).
-- KEYS[1] position hash, KEYS[2] meta hash.
-- ARGV: quantity, revision, deleted (0/1), changed_at_ms, applied_at_ms.
-- Returns {applied (1/0), quantity, revision, deleted, changed_at_ms, applied_at_ms, previous_quantity} of the CURRENT
-- stored state. previous_quantity is the quantity before this change when applied (false = nil reply when the position
-- was new), always false when not applied.
-- Revisions are integers below 2^53, exact as Lua numbers.
local stored = redis.call('HGET', KEYS[1], 'revision')
if stored and tonumber(stored) >= tonumber(ARGV[2]) then
  local h = redis.call('HMGET', KEYS[1], 'quantity', 'revision', 'deleted', 'changed_at_ms', 'applied_at_ms')
  return {0, tonumber(h[1]), tonumber(h[2]), tonumber(h[3]), tonumber(h[4]), tonumber(h[5]), false}
end
local prev = redis.call('HGET', KEYS[1], 'quantity')
redis.call('HSET', KEYS[1],
  'quantity', ARGV[1], 'revision', ARGV[2], 'deleted', ARGV[3],
  'changed_at_ms', ARGV[4], 'applied_at_ms', ARGV[5])
redis.call('HSET', KEYS[2], 'last_revision', ARGV[2], 'last_applied_at_ms', ARGV[5])
return {1, tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5]), prev and tonumber(prev) or false}

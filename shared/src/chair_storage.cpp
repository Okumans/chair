#include "chair_storage.h"

namespace SmartChair {
  namespace {
    constexpr uint32_t LEGACY_FORMAT  = 0x31484353;
    constexpr uint32_t CURRENT_FORMAT = 0x32484353;
  }  // namespace

  std::vector<uint8_t> encodeDurable(const Durable& d) {
    Writer w;
    w.put(CURRENT_FORMAT, 4);
    w.put(d.boot_generation, 8);
    w.put(d.config.revision, 4);
    w.put(d.config.heartbeat_s, 4);
    w.put(d.config.movement_batch_s, 4);
    w.put(d.config.movement_enabled, 1);
    w.put(d.outbox.size(), 1);
    for (const auto& f : d.outbox) {
      const auto bytes = encode(f);
      w.put(bytes.size(), 2);
      w.bytes.insert(w.bytes.end(), bytes.begin(), bytes.end());
    }
    w.put(d.commands.size(), 1);
    for (const auto& c : d.commands) {
      for (auto b : c.uuid) w.put(b, 1);
      w.put(c.config.revision, 4);
      w.put(c.config.heartbeat_s, 4);
      w.put(c.config.movement_batch_s, 4);
      w.put(c.config.movement_enabled, 1);
      w.put(uint8_t(c.status), 1);
    }
    return w.bytes;
  }

  bool decodeDurable(std::span<const uint8_t> bytes, Durable& out) {
    Reader r{bytes};
    const auto format = r.get(4);
    if (format != LEGACY_FORMAT && format != CURRENT_FORMAT) return false;

    Durable d;
    if (format == CURRENT_FORMAT) d.boot_generation = r.get(8);
    if (d.boot_generation > MAX_BOOT_GENERATION) return false;
    d.config.revision         = r.get(4);
    d.config.heartbeat_s      = r.get(4);
    d.config.movement_batch_s = r.get(4);
    auto enabled              = r.get(1);
    if (enabled > 1) return false;
    d.config.movement_enabled = enabled;
    auto count                = r.get(1);
    if (count > Limits::OUTBOX_CAPACITY || !valid(d.config)) return false;
    for (size_t i = 0; i < count; ++i) {
      auto length = r.get(2);
      if (!r.ok || length > bytes.size() - r.pos) return false;
      Frame f;
      if (!decode(bytes.subspan(r.pos, length), f) || !reliable(f.type)) return false;
      r.pos += length;
      d.outbox.push_back(std::move(f));
    }
    count = r.get(1);
    if (count > Limits::COMMAND_RECORDS) return false;
    for (size_t i = 0; i < count; ++i) {
      CommandRecord c;
      for (auto& b : c.uuid) b = r.get(1);
      c.config.revision         = r.get(4);
      c.config.heartbeat_s      = r.get(4);
      c.config.movement_batch_s = r.get(4);
      enabled                   = r.get(1);
      if (enabled > 1) return false;
      c.config.movement_enabled = enabled;
      c.status                  = Status(r.get(1));
      if (!nonzero(c.uuid) || !valid(c.config) || c.status > Status::command_conflict) return false;
      d.commands.push_back(c);
    }
    if (!r.ok || r.pos != bytes.size()) return false;
    out = std::move(d);
    return true;
  }
}  // namespace SmartChair

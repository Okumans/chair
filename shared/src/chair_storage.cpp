#include "chair_storage.h"

namespace SmartChair {
  namespace {
    constexpr uint32_t CURRENT_FORMAT = 0x33484353;

    void writeConfig(Writer& w, const Configuration& c) {
      w.put(c.revision, 4);
      w.put(c.heartbeat_s, 4);
      w.put(c.movement_batch_s, 4);
      w.put(c.movement_enabled, 1);
      w.put(c.radio_windows_enabled, 1);
      w.put(c.radio_interval_s, 4);
      w.put(c.radio_open_s, 4);
      w.put(c.radio_motion_tail_s, 4);
    }

    bool readConfig(Reader& r, Configuration& c) {
      c.revision              = r.get(4);
      c.heartbeat_s           = r.get(4);
      c.movement_batch_s      = r.get(4);
      const auto movement     = r.get(1);
      const auto windows      = r.get(1);
      c.movement_enabled      = movement;
      c.radio_windows_enabled = windows;
      c.radio_interval_s      = r.get(4);
      c.radio_open_s          = r.get(4);
      c.radio_motion_tail_s   = r.get(4);
      return r.ok && movement <= 1 && windows <= 1 && valid(c);
    }
  }  // namespace

  std::vector<uint8_t> encodeDurable(const Durable& d) {
    Writer w;
    w.put(CURRENT_FORMAT, 4);
    w.put(d.boot_generation, 8);
    writeConfig(w, d.config);
    w.put(d.outbox.size(), 1);
    for (const auto& f : d.outbox) {
      const auto bytes = encode(f);
      w.put(bytes.size(), 2);
      w.bytes.insert(w.bytes.end(), bytes.begin(), bytes.end());
    }
    w.put(d.commands.size(), 1);
    for (const auto& c : d.commands) {
      for (auto b : c.uuid) w.put(b, 1);
      writeConfig(w, c.config);
      w.put(uint8_t(c.status), 1);
    }
    return w.bytes;
  }

  bool decodeDurable(std::span<const uint8_t> bytes, Durable& out) {
    Reader r{bytes};
    const auto format = r.get(4);
    if (format != CURRENT_FORMAT) return false;

    Durable d;
    d.boot_generation = r.get(8);
    if (d.boot_generation > MAX_BOOT_GENERATION) return false;
    if (!readConfig(r, d.config)) return false;
    auto count = r.get(1);
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
      if (!readConfig(r, c.config)) return false;
      c.status = Status(r.get(1));
      if (!nonzero(c.uuid) || !valid(c.config) || c.status > Status::command_conflict) return false;
      d.commands.push_back(c);
    }
    if (!r.ok || r.pos != bytes.size()) return false;
    out = std::move(d);
    return true;
  }
}  // namespace SmartChair

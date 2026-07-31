/*
 * ESP32 BLE advertisement scan-and-report firmware (task #63/B5 follow-on).
 *
 * WHAT THIS IS, AND WHAT IT IS NOT
 * =================================
 * This is a SCAN-ONLY firmware. It uses the ESP32's built-in BLE controller
 * (via the Arduino-ESP32 `NimBLE-Arduino` or stock `BLEDevice` scan API) to
 * passively listen for BLE advertisement packets (the standard, legal,
 * receive-only "observer" role defined by the Bluetooth Core Spec, Vol 6,
 * Part B, Sec 4.4.3) and print one JSON object per line to USB-serial for
 * each advertisement seen.
 *
 * It does NOT:
 *   - sniff arbitrary already-connected BLE link-layer traffic (that
 *     requires either a purpose-built LL sniffer such as Nordic's
 *     nRF52840 "BLE Sniffer" firmware + the nRF Sniffer Wireshark plugin
 *     -- a DIFFERENT chip family from this ESP32-S3 board -- or vendor
 *     debug/monitor hooks not exposed by stock Espressif silicon)
 *   - inject, spoof, jam, or transmit anything (RECEIVE ONLY, matching
 *     this project's kismet_bridge.py / hackrf_rx.py convention)
 *   - decode GATT services/characteristics of any device (advertisement
 *     data only: MAC, RSSI, advertised name, service UUIDs if present)
 *
 * This is a clean-room implementation against the public Arduino-ESP32
 * BLE scan API (BLEDevice::getScan()->start(), BLEAdvertisedDeviceCallbacks)
 * -- not derived from or copying any GPL/other-licensed sniffer project.
 *
 * Output format (one line per advertisement, newline-terminated JSON):
 *   {"mac":"AA:BB:CC:DD:EE:FF","rssi":-67,"name":"MyDevice","uuids":["180d"],"ts_ms":123456}
 *
 * `ts_ms` is millis() since this firmware booted (NOT wall-clock time --
 * the ESP32 has no RTC battery here). The host-side bridge script
 * (field-bridge/esp32_ble_scan_bridge.py, not yet built) is responsible
 * for stamping wall-clock receipt time; it must NOT trust ts_ms as
 * absolute time.
 *
 * Board: ESP32-S3 (confirmed via esptool chip_id during recon,
 * 2026-07-28: "ESP32-S3 (QFN56) rev v0.2", 8MB PSRAM, native
 * USB-Serial/JTAG on /dev/ttyACM0, MAC b8:f8:62:f9:54:64). Native USB
 * CDC-ACM is used for serial I/O -- no separate CP2102/FTDI bridge chip
 * needed on this board (confirmed: it enumerates purely as
 * 303a:1001 "Espressif USB JTAG/serial debug unit", CDC-ACM class).
 *
 * NOT YET FLASHED as of this writing -- the board currently runs its
 * original factory/vendor test image ("ESP32_TEST_SEVER" WIFI+BLE
 * burn-in test, read from flash offset 0x10000 during recon, built with
 * arduino-esp32 lib-builder against IDF v4.4.1-472-gc9140caf8c). Flashing
 * this sketch will OVERWRITE that factory test image. Confirm with the
 * project owner before flashing -- see
 * field-bridge/ESP32_BLE_SNIFFER_INTEGRATION.md Sec. "Firmware plan".
 */

#include <BLEDevice.h>
#include <BLEScan.h>
#include <BLEAdvertisedDevice.h>

// Continuous scan, no active scan-request (passive scan avoids
// transmitting SCAN_REQ packets -- keeps this firmware receive-only in
// spirit, at the cost of not seeing scan-response-only fields like some
// device names). Set to true only if active scanning is later deemed
// necessary and approved.
static const bool USE_ACTIVE_SCAN = false;
static const uint32_t SCAN_WINDOW_MS = 1000;   // one scan cycle duration
static const uint32_t SCAN_INTERVAL_MS = 1000; // matches window: back-to-back

BLEScan *pBLEScan = nullptr;

class AdvCallback : public BLEAdvertisedDeviceCallbacks {
  void onResult(BLEAdvertisedDevice advertisedDevice) override {
    // Build one JSON line manually (no ArduinoJson dependency -- keeps
    // this firmware's library surface minimal and auditable).
    String mac = advertisedDevice.getAddress().toString().c_str();
    int rssi = advertisedDevice.haveRSSI() ? advertisedDevice.getRSSI() : 0;

    String name = "";
    if (advertisedDevice.haveName()) {
      name = advertisedDevice.getName().c_str();
      name.replace("\"", "'"); // crude JSON-safety, no embedded quotes expected
    }

    String uuids = "";
    if (advertisedDevice.haveServiceUUID()) {
      // Only the first UUID is surfaced here; extend if multi-UUID
      // devices become operationally relevant.
      uuids = advertisedDevice.getServiceUUID().toString().c_str();
    }

    Serial.print("{\"mac\":\"");
    Serial.print(mac);
    Serial.print("\",\"rssi\":");
    Serial.print(rssi);
    Serial.print(",\"name\":\"");
    Serial.print(name);
    Serial.print("\",\"uuids\":[");
    if (uuids.length() > 0) {
      Serial.print("\"");
      Serial.print(uuids);
      Serial.print("\"");
    }
    Serial.print("],\"ts_ms\":");
    Serial.print(millis());
    Serial.println("}");
  }
};

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println("{\"event\":\"boot\",\"firmware\":\"esp32_ble_scan_firmware\",\"role\":\"scan_only\"}");

  BLEDevice::init("");
  pBLEScan = BLEDevice::getScan();
  pBLEScan->setAdvertisedDeviceCallbacks(new AdvCallback(), /*wantDuplicates=*/true);
  pBLEScan->setActiveScan(USE_ACTIVE_SCAN);
  pBLEScan->setInterval(100);
  pBLEScan->setWindow(99); // must be <= interval
}

void loop() {
  // start() blocks for the given duration then returns; results are
  // streamed live via onResult() as they arrive, not batched at the end.
  pBLEScan->start(SCAN_WINDOW_MS / 1000, false);
  pBLEScan->clearResults();
  delay(SCAN_INTERVAL_MS - SCAN_WINDOW_MS > 0 ? SCAN_INTERVAL_MS - SCAN_WINDOW_MS : 0);
}

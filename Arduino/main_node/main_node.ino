#include <Arduino.h>
#include <Wire.h>
#include "LittleFS.h"
#include "DHT.h"
#include "LoRaWan_APP.h"   // Heltec V3 (SX1262) Radio API

// pins
#define PIN_DHT      4
#define PIN_SOIL1    5
#define PIN_SOIL2    6
#define PIN_LDR      3

#define I2C_SDA      2
#define I2C_SCL      1

#define DHTTYPE DHT11
DHT dht(PIN_DHT, DHTTYPE);

// logging config
const char* LOG_PATH = "/plant.csv";
const uint32_t LOG_INTERVAL_MS = 30UL * 60UL * 1000UL;  // 30 Minuten

// watering thresholds
const int SOIL_THRESHOLD = 1800;                // wenn DARÜBER => trocken => gießen
const uint32_t PUMP_ON_MS = 4000;               // 4s
const uint32_t PUMP_COOLDOWN_MS = 5UL * 60UL * 1000UL;

bool wateringArmed = false;
uint32_t lastLogMs = 0;
uint32_t nextWaterCheckMs = 0;
String cmd;

// ADC helper
static inline int readADC(int pin) { return analogRead(pin); }

// CSV file handling
void ensureHeader() {
  if (!LittleFS.exists(LOG_PATH)) {
    File f = LittleFS.open(LOG_PATH, "w");
    if (f) {
      // soil_ref = soil2 (Basis fürs Gießen)
      f.println("ts,temp_c,hum_pct,soil1,soil2,soil_ref,light_raw,pump_event");
      f.close();
    }
  }
}
void appendLine(const String &line) {
  File f = LittleFS.open(LOG_PATH, "a");
  if (!f) return;
  f.print(line);
  f.close();
}

// LoRa config
static const uint32_t LORA_FREQ = 868E6;
static const uint8_t  LORA_SF   = 7;
static const uint32_t LORA_BW   = 0;      // 0=125kHz
static const uint8_t  LORA_CR   = 1;      // 4/5
static const uint16_t LORA_PREAMBLE = 8;
static const int8_t   LORA_TX_POWER = 14;

static RadioEvents_t RadioEvents;
static bool txBusy = false;

// last received line
static String lastRxLine = "";
static uint32_t lastRxAtMs = 0;

void OnTxDone(void)    { txBusy = false; Radio.Rx(0); }
void OnTxTimeout(void) { txBusy = false; Radio.Rx(0); }

void OnRxDone(uint8_t *payload, uint16_t size, int16_t rssi, int8_t snr) {
  if (size == 0) { Radio.Rx(0); return; }

  static uint8_t buf[256];
  uint16_t n = min<uint16_t>(size, sizeof(buf)-1);
  memcpy(buf, payload, n);
  buf[n] = 0;

  lastRxLine = String((char*)buf);
  lastRxLine.trim();
  lastRxAtMs = millis();

  Serial.print("LORA RX: ");
  Serial.println(lastRxLine);

  Radio.Rx(0);
}
void OnRxTimeout(void){ Radio.Rx(0); }
void OnRxError(void)  { Radio.Rx(0); }

void setupLoRa() {
  #ifndef HELTEC_BOARD
    #define HELTEC_BOARD 0
  #endif
  Mcu.begin(HELTEC_BOARD, 0);

  RadioEvents.TxDone    = OnTxDone;
  RadioEvents.TxTimeout = OnTxTimeout;
  RadioEvents.RxDone    = OnRxDone;
  RadioEvents.RxTimeout = OnRxTimeout;
  RadioEvents.RxError   = OnRxError;

  Radio.Init(&RadioEvents);
  Radio.SetChannel(LORA_FREQ);

  Radio.SetTxConfig(MODEM_LORA, LORA_TX_POWER, 0, LORA_BW, LORA_SF, LORA_CR,
                    LORA_PREAMBLE, false, true, 0, 0, false, 3000);

  Radio.SetRxConfig(MODEM_LORA, LORA_BW, LORA_SF, LORA_CR, 0, LORA_PREAMBLE,
                    0, false, 0, true, 0, 0, false, true);

  Radio.Rx(0);
}

bool sendLoRaLine(const String &line) {
  if (txBusy) return false;
  txBusy = true;
  String out = line + "\n";
  Radio.Send((uint8_t*)out.c_str(), out.length());
  return true;
}

// send cmd, wait for response starting with prefix
bool loraRequest(const String &cmdLine, const String &expectPrefix, String &outLine, uint32_t timeoutMs) {
  lastRxLine = "";
  lastRxAtMs = 0;

  if (!sendLoRaLine(cmdLine)) return false;

  uint32_t start = millis();
  while (millis() - start < timeoutMs) {
    Radio.IrqProcess();
    if (lastRxLine.length()) {
      if (expectPrefix.length() == 0 || lastRxLine.startsWith(expectPrefix)) {
        outLine = lastRxLine;
        return true;
      }
      lastRxLine = "";
    }
    delay(5);
  }
  return false;
}

// get timestamp from Node2 via LoRa, fallback to millis
String getTimestampSafe() {
  String resp;
  if (loraRequest("GET TIME", "TIME ", resp, 1200)) {
    return resp.substring(5); // "TIME " weg
  }
  char b[32];
  snprintf(b, sizeof(b), "millis:%lu", (unsigned long)millis());
  return String(b);
}

// sensor reading
void readSensors(float &temp, float &hum, int &soil1, int &soil2, int &soilRef, int &light) {
  temp = dht.readTemperature();
  hum  = dht.readHumidity();
  if (isnan(temp)) temp = -1;
  if (isnan(hum))  hum  = -1;

  soil1 = readADC(PIN_SOIL1);
  soil2 = readADC(PIN_SOIL2);

  soilRef = soil2; // <<< nur Soil2 als Referenz fürs Gießen

  light = readADC(PIN_LDR);
}

// trigger pump on Node2 via LoRa if soil is dry
bool maybeWaterRemote(int soilRef) {
  if (!wateringArmed) return false;
  if (millis() < nextWaterCheckMs) return false;
  if (soilRef <= SOIL_THRESHOLD) return false;

  Serial.printf("PUMP: soil2 %d > %d => watering\n", soilRef, SOIL_THRESHOLD);

  String resp;
  loraRequest("PUMP ON", "ACK", resp, 1200);
  delay(PUMP_ON_MS);
  loraRequest("PUMP OFF", "ACK", resp, 1200);

  nextWaterCheckMs = millis() + PUMP_COOLDOWN_MS;
  return true;
}

// serial command handlers
void cmdDump(){
  File f = LittleFS.open(LOG_PATH, "r");
  if (!f) { Serial.println("ERR"); return; }
  Serial.println("=== BEGIN CSV ===");
  while (f.available()) Serial.write(f.read());
  Serial.println("\n=== END CSV ===");
  f.close();
}
void cmdClear(){
  if (LittleFS.exists(LOG_PATH)) LittleFS.remove(LOG_PATH);
  ensureHeader();
  Serial.println("OK");
}

void cmdStatus(){
  float t,h;
  int s1,s2,sref,ldr;
  readSensors(t,h,s1,s2,sref,ldr);

  String ts = getTimestampSafe();

  Serial.println("STATUS:");
  Serial.print("Time: "); Serial.println(ts);
  Serial.print("Watering: "); Serial.println(wateringArmed ? "ARMED" : "DISARMED");
  Serial.printf("Air Temp: %.1f C\n", t);
  Serial.printf("Air Hum:  %.1f %%\n", h);
  Serial.printf("Soil1: %d\n", s1);
  Serial.printf("Soil2: %d\n", s2);
  Serial.printf("SoilRef (Soil2): %d\n", sref);
  Serial.printf("Light: %d\n", ldr);
  Serial.printf("Next water in: %lus\n",
    (unsigned long)((nextWaterCheckMs > millis()) ? (nextWaterCheckMs - millis())/1000 : 0));
  Serial.println("Commands: STATUS | ARM | DISARM | PUMP ON | PUMP OFF | DUMP | CLEAR");
}

void cmdPumpOn(){
  String resp;
  bool ok = loraRequest("PUMP ON", "ACK", resp, 1200);
  Serial.println(ok ? "OK: sent PUMP ON" : "ERR: no ACK");
}
void cmdPumpOff(){
  String resp;
  bool ok = loraRequest("PUMP OFF", "ACK", resp, 1200);
  Serial.println(ok ? "OK: sent PUMP OFF" : "ERR: no ACK");
}

void handleCommand(String line){
  line.trim();
  String up = line; up.toUpperCase();

  if (up == "STATUS") { cmdStatus(); return; }
  if (up == "DUMP")   { cmdDump(); return; }
  if (up == "CLEAR")  { cmdClear(); return; }
  if (up == "ARM")    { wateringArmed = true; Serial.println("OK: ARMED"); return; }
  if (up == "DISARM") { wateringArmed = false; Serial.println("OK: DISARMED"); return; }
  if (up == "PUMP ON")  { cmdPumpOn(); return; }
  if (up == "PUMP OFF") { cmdPumpOff(); return; }

  Serial.print("Unknown cmd: "); Serial.println(line);
}

void setup(){
  Serial.begin(115200);
  delay(800);

  Wire.begin(I2C_SDA, I2C_SCL);
  Wire.setClock(100000);

  dht.begin();
  analogReadResolution(12);
  analogSetAttenuation(ADC_11db);

  LittleFS.begin(true);
  ensureHeader();

  setupLoRa();

  lastLogMs = millis();
  nextWaterCheckMs = millis();

  Serial.println("READY (MAIN Logger + LoRa client) - watering based on Soil2");
  Serial.println("Commands: STATUS | ARM | DISARM | PUMP ON | PUMP OFF | DUMP | CLEAR");
  cmdStatus();
}

void loop(){
  Radio.IrqProcess();

  if (millis() - lastLogMs >= LOG_INTERVAL_MS) {
    lastLogMs += LOG_INTERVAL_MS;

    float temp, hum;
    int soil1, soil2, soilRef, light_raw;
    readSensors(temp, hum, soil1, soil2, soilRef, light_raw);

    bool pumped = maybeWaterRemote(soilRef);

    String ts = getTimestampSafe();

    char buf[200];
    snprintf(buf, sizeof(buf),
             "%s,%.1f,%.1f,%d,%d,%d,%d,%d\n",
             ts.c_str(),
             temp, hum,
             soil1, soil2, soilRef,
             light_raw,
             pumped ? 1 : 0);

    Serial.print("LOG: ");
    Serial.print(buf);
    appendLine(String(buf));
  }

  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (cmd.length()) {
        String line = cmd;
        cmd = "";
        handleCommand(line);
      }
    } else {
      cmd += c;
      if (cmd.length() > 200) cmd = "";
    }
  }
}
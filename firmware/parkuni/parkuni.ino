// ParkUni — ESP32 con 4 sensores ultrasónicos y 4 LED RGB (prototipo).
//
// Combina el cableado probado del prototipo (ver docs/Ficha_Tecnica_Estacionamiento.docx) con
// el reporte al servidor Ubuntu:
//   * LED: VERDE = libre, ROJO = ocupado (el azul solo se usa en la emergencia, junto al rojo = MORADO).
//   * Cajón 1 = sensor AJ-SR04M impermeable (límite 50 cm); cajones 2-4 = HC-SR04 (límite 10 cm).
//   * Reporta por HTTP al servidor (POST /api/sensor) y de la misma respuesta lee si hay EMERGENCIA:
//     en ese caso los 4 LED parpadean en MORADO (rojo + azul) hasta que el administrador la desactive.
//   * Sin WiFi o sin servidor los LED siguen funcionando solos con los sensores.
//
// Cableado (no cambiar): ECHO por divisor de voltaje 1k + 2k (de 5 V a 3.3 V); sensores a VIN (5 V);
// cátodos de los LED RGB y tierras comunes a GND. LED de cátodo común: HIGH = encendido.

#include <WiFi.h>
#include <HTTPClient.h>

// ======================= RED Y SERVIDOR =======================
const char* WIFI_SSID  = "Anahuac";                                 // red abierta, sin contraseña
const char* SERVER_URL = "http://172.20.5.57:8000/api/sensor";      // IP de la compu del servidor (cámbiala si cambia)

const unsigned long SEND_MS    = 1500;   // cada cuánto se reporta al servidor
const unsigned long MEASURE_MS = 600;    // cada cuánto se mide

// ======================= SENSORES Y LEDs (cableado real) =======================
const int N = 4;
const char* IDS[N] = { "A1", "A2", "A3", "A4" };   // deben coincidir con MAP.sensors (public/js/map-data.js)

const int trigPins[N]  = { 23, 22, 21, 19 };
const int echoPins[N]  = { 34, 35, 36, 39 };

const int ledReds[N]   = { 32, 26, 16, 5 };
const int ledGreens[N] = { 25, 27, 18, 2 };
const int ledBlues[N]  = { 33, 14, 17, 4 };

// Límite de detección por cajón: ocupado si mide ≤ límite.
const int LIMITES[N]   = { 50, 10, 10, 10 };      // cm  (cajón 1 = impermeable)
const int HISTERESIS   = 3;                       // cm de margen para volver a "libre" (evita parpadeo)

bool occupied[N];
int  distanceCm[N];                               // 0 = sin eco (fuera de rango)
bool emergency = false;
unsigned long lastMeasure = 0, lastSend = 0;

// ======================= LED =======================
void setColor(int i, bool r, bool g, bool b) {
  digitalWrite(ledReds[i],   r ? HIGH : LOW);
  digitalWrite(ledGreens[i], g ? HIGH : LOW);
  digitalWrite(ledBlues[i],  b ? HIGH : LOW);
}

void updateLeds() {
  if (emergency) {
    bool on = (millis() / 500) % 2 == 0;                      // 1 s por ciclo
    for (int i = 0; i < N; i++) setColor(i, on, false, on);   // morado = rojo + azul
  } else {
    for (int i = 0; i < N; i++) {
      if (occupied[i]) setColor(i, true, false, false);       // ROJO  (ocupado)
      else             setColor(i, false, true, false);       // VERDE (libre)
    }
  }
}

// ======================= SENSOR =======================
int readCm(int i) {
  digitalWrite(trigPins[i], LOW);  delayMicroseconds(5);
  digitalWrite(trigPins[i], HIGH); delayMicroseconds(10);
  digitalWrite(trigPins[i], LOW);
  long us = pulseIn(echoPins[i], HIGH, 30000UL);              // 0 = sin eco
  return us == 0 ? 0 : (int)(us * 0.034 / 2);
}

int median3(int i) {                                          // mediana de 3 lecturas: filtra ruido
  int a = readCm(i); delay(30);
  int b = readCm(i); delay(30);
  int c = readCm(i);
  if (a > b) { int t = a; a = b; b = t; }
  if (b > c) b = c;
  return a > b ? a : b;
}

void measure() {
  for (int i = 0; i < N; i++) {
    int cm = median3(i);
    distanceCm[i] = (cm > 400) ? 0 : cm;                      // más de 4 m = fuera de rango
    if (cm > 0 && cm <= LIMITES[i])               occupied[i] = true;
    else if (cm == 0 || cm > LIMITES[i] + HISTERESIS) occupied[i] = false;   // sin eco = nada enfrente = libre

    Serial.printf("Cajon %d: ", i + 1);
    if (distanceCm[i] == 0) Serial.print("Fuera de rango");
    else                    Serial.printf("%d cm", distanceCm[i]);
    Serial.println(occupied[i] ? " ---> ROJO (Ocupado)" : " ---> VERDE (Libre)");

    updateLeds();                                             // mantiene el parpadeo de emergencia mientras se mide
    delay(100);                                               // que un sensor no interfiera con el siguiente
  }
  Serial.println("-------------------------");
}

// ======================= RED =======================
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < 8000) { delay(200); updateLeds(); }
}

void report() {
  if (WiFi.status() != WL_CONNECTED) return;
  String json = "{\"sensors\":{";
  for (int i = 0; i < N; i++) {
    json += "\""; json += IDS[i]; json += "\":{\"occupied\":";
    json += occupied[i] ? "true" : "false";
    json += ",\"distance_cm\":"; json += distanceCm[i]; json += "}";
    if (i < N - 1) json += ",";
  }
  json += "}}";

  HTTPClient http;
  http.begin(SERVER_URL);
  http.setTimeout(2500);
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(json);
  if (code == 200) {
    String body = http.getString();                           // {"ok": true, "emergency": false}
    emergency = body.indexOf("\"emergency\": true") >= 0 || body.indexOf("\"emergency\":true") >= 0;
  }
  http.end();
  Serial.printf("POST -> %d  emergencia=%d\n", code, emergency);
}

// ======================= SETUP / LOOP =======================
void setup() {
  Serial.begin(115200);
  for (int i = 0; i < N; i++) {
    pinMode(trigPins[i], OUTPUT);
    pinMode(echoPins[i], INPUT);
    pinMode(ledReds[i], OUTPUT);
    pinMode(ledGreens[i], OUTPUT);
    pinMode(ledBlues[i], OUTPUT);
    setColor(i, false, false, false);                         // apagados al inicio
    occupied[i] = false; distanceCm[i] = 0;
  }
  Serial.println("ParkUni listo. Cajon 1 a 50 cm | Cajones 2, 3 y 4 a 10 cm");
  connectWiFi();
}

void loop() {
  unsigned long now = millis();
  if (now - lastMeasure >= MEASURE_MS) { measure(); lastMeasure = millis(); }
  if (now - lastSend >= SEND_MS) { connectWiFi(); report(); lastSend = millis(); }
  updateLeds();                                               // el parpadeo de emergencia usa millis()
  delay(20);
}

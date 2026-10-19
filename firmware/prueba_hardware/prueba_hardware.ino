// Prueba de hardware (sin WiFi): sensores ultrasónicos + LED RGB.
// Código original del compañero que cableó el prototipo. Sirve para comprobar el cableado
// antes de cargar firmware/parkuni/parkuni.ino.

const int numCajones = 4;

// Pines de los sensores
const int trigPins[]  = {23, 22, 21, 19}; 
const int echoPins[]  = {34, 35, 36, 39}; 

// Pines de los LEDs
const int ledReds[]   = {32, 26, 16, 5};
const int ledGreens[] = {25, 27, 18, 2};
const int ledBlues[]  = {33, 14, 17, 4};

// Límites de distancia: 50cm para Cajón 1, 10cm para el resto
const int limites[] = {50, 10, 10, 10};

void setup() {
  Serial.begin(115200);

  for (int i = 0; i < numCajones; i++) {
    pinMode(trigPins[i], OUTPUT);
    pinMode(echoPins[i], INPUT);
    
    pinMode(ledReds[i], OUTPUT);
    pinMode(ledGreens[i], OUTPUT);
    pinMode(ledBlues[i], OUTPUT);

    // Apagar todos los colores al inicio
    digitalWrite(ledReds[i], LOW);
    digitalWrite(ledGreens[i], LOW);
    digitalWrite(ledBlues[i], LOW);
  }
  
  Serial.println("Sistema Completo Listo...");
  Serial.println("Cajon 1 a 50cm | Cajones 2, 3 y 4 a 10cm");
  delay(1000);
}

void loop() {
  for (int i = 0; i < numCajones; i++) {
    // 1. Disparo limpio
    digitalWrite(trigPins[i], LOW);
    delayMicroseconds(5);
    digitalWrite(trigPins[i], HIGH);
    delayMicroseconds(10);
    digitalWrite(trigPins[i], LOW);

    // 2. Lectura
    long duracion = pulseIn(echoPins[i], HIGH, 30000); 
    long distancia = 999; 

    if (duracion > 0) {
      distancia = duracion * 0.034 / 2;
    }

    // 3. Monitor Serie
    Serial.print("Cajon ");
    Serial.print(i + 1);
    Serial.print(": ");

    if (distancia == 999 || distancia > 400) {
      Serial.print("Fuera de rango ---> ");
    } else {
      Serial.print(distancia);
      Serial.print(" cm ---> ");
    }

    // 4. Lógica de Colores Probada
    if (distancia > 0 && distancia <= limites[i]) {
      Serial.println("ROJO (Ocupado)");
      digitalWrite(ledReds[i], HIGH);
      digitalWrite(ledGreens[i], LOW);
      digitalWrite(ledBlues[i], LOW); 
    } else {
      Serial.println("VERDE (Libre)");
      digitalWrite(ledReds[i], LOW);
      digitalWrite(ledGreens[i], HIGH);
      digitalWrite(ledBlues[i], LOW); 
    }
    
    // Pausa para que los sensores no interfieran entre sí
    delay(100); 
  }
  
  Serial.println("-------------------------");
  delay(500); 
}

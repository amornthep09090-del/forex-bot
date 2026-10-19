# ใช้ Python 3.11 แบบ Slim ที่เสถียรและกินทรัพยากรน้อยบน Railway
FROM python:3.11-slim

# ตั้งค่าให้แสดงผล Log ทันทีแบบ Real-time และไม่สร้างไฟล์ขยะ .pyc
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Bangkok

# ติดตั้งไลบรารีพื้นฐานของระบบและ Timezone (จำเป็นสำหรับเช็กเวลาตลาด New York / UTC)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# กำหนดโฟลเดอร์ทำงานและสร้างโฟลเดอร์ /data รอไว้สำหรับผูก Railway Volume
WORKDIR /app
RUN mkdir -p /data

# คัดลอกและติดตั้งแพ็กเกจ Python
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# คัดลอกไฟล์โค้ดทั้งหมดเข้าคอนเทนเนอร์
COPY . .

# รันบอท (หากไฟล์โค้ดของคุณไม่ได้ชื่อ main.py ให้เปลี่ยนชื่อตรงนี้)
CMD ["python", "main.py"]

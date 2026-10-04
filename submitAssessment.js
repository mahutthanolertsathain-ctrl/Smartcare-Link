// 1. จัดเตรียมข้อมูล JSON
const payload = {
    patient_id: patientData.hn,
    assessment_timestamp: new Date().toISOString(),
    triage_data: {
        is_immediate_red_flag: false, // ถ้ามาจากหน้าฉุกเฉินจะเป็น true
        main_category: patientData.category,
        symptom_name: patientData.symptomName,
        sub_answers: {
            q1: getRadioVal('q1'),
            q2: patientData.category === 'Musculoskeletal' ? getRadioVal('q2') : (getRadioVal('q2') || getCheckVals('q2')),
            q3: patientData.category === 'Musculoskeletal' ? getCheckVals('q3') : (getRadioVal('q3') || getCheckVals('q3'))
        }
    },
    computed_result: {
        triage_level: level,
        recommended_action: actionText
    }
};

// 2. ยิงข้อมูลไปที่ API หลังบ้าน
/* fetch('https://api.yourhospital.com/v1/triage', {
    method: 'POST',
    headers: {
        'Content-Type': 'application/json'
    },
    body: JSON.stringify(payload)
})
.then(response => response.json())
.then(data => {
    console.log('บันทึกข้อมูลสำเร็จ:', data);
    // กรณีที่หลังบ้านส่งเลขคิวกลับมา เราสามารถอัปเดตเลขคิวที่นี่ได้ เช่น
    // document.getElementById('queue-number').innerText = data.queue_number;
    goToStep(5);
})
.catch((error) => {
    console.error('เกิดข้อผิดพลาด:', error);
    alert('ไม่สามารถเชื่อมต่อเซิร์ฟเวอร์ได้ กรุณาลองใหม่อีกครั้ง');
});
*/
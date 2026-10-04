(() => {
  const valid = (lat, lng) => lat != null && lng != null && Number.isFinite(Number(lat)) && Number.isFinite(Number(lng)) && lat !== '' && lng !== '' && Number(lat) >= -90 && Number(lat) <= 90 && Number(lng) >= -180 && Number(lng) <= 180;

  window.createLocationPicker = function ({containerId, latitudeInputId, longitudeInputId, statusId, locateButtonId, initialLatitude, initialLongitude, onLocation}) {
    const container = document.getElementById(containerId);
    const latitudeInput = document.getElementById(latitudeInputId);
    const longitudeInput = document.getElementById(longitudeInputId);
    const status = document.getElementById(statusId);
    const button = locateButtonId ? document.getElementById(locateButtonId) : null;
    let map = null;
    let marker = null;

    const showStatus = message => {
      if (status) status.textContent = message;
    };

    const setLocation = (lat, lng, pan = true) => {
      if (lat == null || lng == null || lat === '' || lng === '') {
        latitudeInput.value = '';
        longitudeInput.value = '';
        if (map && marker) { map.removeLayer(marker); marker = null; }
        showStatus('ยังไม่ได้เลือกตำแหน่ง');
        onLocation?.(null);
        return;
      }
      lat = Number(lat);
      lng = Number(lng);
      if (!valid(lat, lng)) return;
      latitudeInput.value = lat.toFixed(6);
      longitudeInput.value = lng.toFixed(6);
      if (map) {
        if (!marker) {
          marker = L.marker([lat, lng], {draggable: true}).addTo(map);
          marker.on('dragend', () => { const p = marker.getLatLng(); setLocation(p.lat, p.lng, false); });
        } else marker.setLatLng([lat, lng]);
        if (pan) map.setView([lat, lng], 17);
      }
      showStatus('พิกัดที่เลือก ' + lat.toFixed(6) + ', ' + lng.toFixed(6));
      onLocation?.({latitude: lat, longitude: lng});
      latitudeInput.dispatchEvent(new Event('input', {bubbles: true}));
      longitudeInput.dispatchEvent(new Event('input', {bubbles: true}));
    };

    // Keep GPS usable even if the map library or map tiles fail to load.
    if (container && latitudeInput && longitudeInput && window.L) {
      map = L.map(container, {scrollWheelZoom: false}).setView([13.7563, 100.5018], 5);
      L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>'
      }).addTo(map);
      map.on('click', event => setLocation(event.latlng.lat, event.latlng.lng, false));
      requestAnimationFrame(() => map.invalidateSize());
      window.addEventListener('resize', () => map.invalidateSize());
      if (valid(initialLatitude, initialLongitude)) setLocation(initialLatitude, initialLongitude, true);
    } else if (!window.L) {
      showStatus('แผนที่โหลดไม่สำเร็จ แต่ยังลองใช้ตำแหน่ง GPS ได้');
    }

    if (button) {
      button.addEventListener('click', () => {
        if (!window.isSecureContext) {
          showStatus('ต้องเปิดเว็บผ่าน HTTPS จึงจะอ่านตำแหน่งเครื่องได้');
          return;
        }
        if (!navigator.geolocation) {
          showStatus('เบราว์เซอร์นี้ไม่รองรับการอ่านตำแหน่ง ลองเปิดลิงก์ใน Safari หรือ Chrome');
          return;
        }
        button.disabled = true;
        showStatus('กำลังขออนุญาตและอ่านตำแหน่งจากเครื่อง…');
        try {
          navigator.geolocation.getCurrentPosition(position => {
            setLocation(position.coords.latitude, position.coords.longitude, true);
            button.disabled = false;
          }, error => {
            button.disabled = false;
            if (error.code === error.PERMISSION_DENIED) {
              showStatus('เบราว์เซอร์ไม่อนุญาตตำแหน่ง ให้เปิดสิทธิ์ Location ของเว็บนี้ในการตั้งค่าเบราว์เซอร์ แล้วกดอีกครั้ง');
            } else if (error.code === error.POSITION_UNAVAILABLE) {
              showStatus('เครื่องยังระบุตำแหน่งไม่ได้ ตรวจว่าเปิด Location Services และลองอีกครั้ง');
            } else if (error.code === error.TIMEOUT) {
              showStatus('อ่านตำแหน่งใช้เวลานานเกินไป ลองอีกครั้งหรือเลือกตำแหน่งบนแผนที่');
            } else {
              showStatus('อ่านตำแหน่งไม่สำเร็จ ลองอีกครั้งหรือเลือกตำแหน่งบนแผนที่');
            }
          }, {enableHighAccuracy: true, timeout: 20000, maximumAge: 0});
        } catch (_) {
          button.disabled = false;
          showStatus('เบราว์เซอร์บล็อกการอ่านตำแหน่ง ลองเปิดลิงก์นี้ใน Safari หรือ Chrome และอนุญาต Location');
        }
      });
    }

    return {setLocation, clear: () => setLocation(null, null), map};
  };
})();

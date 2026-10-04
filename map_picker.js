(() => {
  const valid = (lat, lng) => lat != null && lng != null && Number.isFinite(Number(lat)) && Number.isFinite(Number(lng)) && lat !== '' && lng !== '' && Number(lat) >= -90 && Number(lat) <= 90 && Number(lng) >= -180 && Number(lng) <= 180;
  window.createLocationPicker = function ({containerId, latitudeInputId, longitudeInputId, statusId, locateButtonId, initialLatitude, initialLongitude, onLocation}) {
    const container = document.getElementById(containerId);
    const latitudeInput = document.getElementById(latitudeInputId);
    const longitudeInput = document.getElementById(longitudeInputId);
    const status = document.getElementById(statusId);
    if (!container || !latitudeInput || !longitudeInput || !window.L) return null;
    const map = L.map(container, {scrollWheelZoom: false}).setView([13.7563, 100.5018], 5);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap contributors</a>'
    }).addTo(map);
    let marker = null;
    const showStatus = (lat, lng) => {
      if (status) status.textContent = lat == null || lng == null ? 'ยังไม่ได้เลือกตำแหน่ง' : `พิกัดที่เลือก ${Number(lat).toFixed(6)}, ${Number(lng).toFixed(6)}`;
    };
    const setLocation = (lat, lng, pan = true) => {
      if (lat == null || lng == null || lat === '' || lng === '') {
        latitudeInput.value = '';
        longitudeInput.value = '';
        if (marker) { map.removeLayer(marker); marker = null; }
        showStatus(null, null);
        onLocation?.(null);
        return;
      }
      lat = Number(lat); lng = Number(lng);
      if (!valid(lat, lng)) return;
      latitudeInput.value = lat.toFixed(6);
      longitudeInput.value = lng.toFixed(6);
      if (!marker) {
        marker = L.marker([lat, lng], {draggable: true}).addTo(map);
        marker.on('dragend', () => { const p = marker.getLatLng(); setLocation(p.lat, p.lng, false); });
      } else marker.setLatLng([lat, lng]);
      if (pan) map.setView([lat, lng], 17);
      showStatus(lat, lng);
      onLocation?.({latitude: lat, longitude: lng});
      latitudeInput.dispatchEvent(new Event('input', {bubbles: true}));
      longitudeInput.dispatchEvent(new Event('input', {bubbles: true}));
    };
    map.on('click', event => setLocation(event.latlng.lat, event.latlng.lng, false));
    if (valid(initialLatitude, initialLongitude)) setLocation(initialLatitude, initialLongitude, true);
    requestAnimationFrame(() => map.invalidateSize());
    window.addEventListener('resize', () => map.invalidateSize());
    if (locateButtonId) {
      const button = document.getElementById(locateButtonId);
      button?.addEventListener('click', () => {
        if (!navigator.geolocation) return alert('เบราว์เซอร์นี้ไม่รองรับการระบุตำแหน่ง');
        button.disabled = true;
        if (status) status.textContent = 'กำลังอ่านตำแหน่งจากอุปกรณ์…';
        navigator.geolocation.getCurrentPosition(position => {
          setLocation(position.coords.latitude, position.coords.longitude, true);
          button.disabled = false;
        }, () => {
          button.disabled = false;
          if (status) status.textContent = 'อ่านตำแหน่งไม่ได้ ลากแผนที่หรือแตะตำแหน่งบนแผนที่แทนได้';
        }, {enableHighAccuracy: true, timeout: 10000, maximumAge: 30000});
      });
    }
    return {setLocation, clear: () => setLocation(null, null), map};
  };
})();


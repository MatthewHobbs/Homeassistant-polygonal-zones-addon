function create_load_btn() {
    if (document.getElementById('load-btn')) return;
    let load_text = document.createElement('p');
    load_text.innerHTML = 'You can replace every zone with the zones in a GeoJSON file.';
    load_text.id = 'load-text';
    document.querySelector('.header').appendChild(load_text);

    let load_btn = document.createElement('button');
    load_btn.classList.add('btn');
    load_btn.id = 'load-btn';
    load_btn.innerHTML = 'Replace zones from file';
    load_btn.addEventListener('click', function () {
        load_bulk_json();
    });

    document.querySelector('.header').appendChild(load_btn);
}

// A DOM node for a popup or tooltip, so a zone or device name is inserted as
// text: Leaflet renders a string as HTML, which would run script in a crafted
// name.
function text_element(text) {
    let el = document.createElement('span');
    el.textContent = typeof text === 'string' ? text : '';
    return el;
}

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { text_element };
}

function create_load_btn() {
    let load_text = document.createElement('p');
    load_text.innerHTML = 'You can also load bulk zones from a geojson file by clicking the load button.';
    load_text.id = 'load-text';
    document.querySelector('.header').appendChild(load_text);

    let load_btn = document.createElement('button');
    load_btn.classList.add('btn');
    load_btn.id = 'load-btn';
    load_btn.innerHTML = 'Load bulk json';
    load_btn.addEventListener('click', function () {
        load_bulk_json();
    });

    document.querySelector('.header').appendChild(load_btn);
}

function delete_load_btn() {
    document.querySelector('#load-btn').remove();
    document.querySelector('#load-text').remove();

}

// Build a DOM node for a Leaflet popup so feature.properties.name is inserted
// as text, never parsed as HTML. bindPopup(string) would render the string as
// HTML, which would execute script for a crafted zone name in zones.json.
function name_popup_element(feature) {
    let el = document.createElement('span');
    let name = feature && feature.properties ? feature.properties.name : '';
    el.textContent = typeof name === 'string' ? name : '';
    return el;
}

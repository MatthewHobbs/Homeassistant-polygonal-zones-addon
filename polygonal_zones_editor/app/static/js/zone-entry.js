// custom component that will renderder the above template
class ZoneEntry extends HTMLElement {
    styles = `
    <style>
        .hidden { 
            display: none !important;
        }
    
        .header { 
            display: flex;
            justify-content: space-between;
        }
        
        .zone-entry {
            width: 100%;
            margin-top: 2px;
            margin-bottom: 2px;
            
            padding-top: 5px;
            padding-bottom: 5px;
        }
        
        .zone-entry.editing {
            margin: 5px -10px;
            padding: 5px 10px;
            background-color: rgba(0, 0, 0, 0.05);
        }
        
        .zone-entry.editing .header {
            font-weight: bold;
        }
        
        .edit-zone {
            display: flex;
            flex-direction: column;
        }
        
        .edit-zone .properties {
            display: flex;
            justify-content: space-between;
            
            margin-top: 10px;
            margin-bottom: 10px;
        }
        
        .edit-btn {
            background-color: black;
            color: white;
            padding: 0.5ch 2ch;
            border: none;
            border-radius: 2px;
            cursor: pointer;
            width: 100%;
            
            box-shadow: 0 0 2px rgba(0, 0, 0, 0.1);
        }
    </style>
    `;

    constructor() {
        super();
    }

    connectedCallback() {
        this.attachShadow({mode: 'open'});

        // listen to changes of the editing attribute
        this._observer = new MutationObserver((mutations) => {
            mutations.forEach((mutation) => {
                if (mutation.attributeName === 'editing') {
                    this.render(this.getAttribute('editing') === 'true');
                }
            });
        });

        this._observer.observe(this, {attributes: true});

        this.render(false);
    }

    disconnectedCallback() {
        if (this._observer) this._observer.disconnect();
    }

    render(editing) {
        let name = this.getAttribute('name') ?? '';

        // Zone names come from user input in zones.json; insert via DOM APIs
        // rather than innerHTML interpolation so names containing HTML do not
        // execute script. The surrounding markup is static.
        this.shadowRoot.innerHTML = `
            ${this.styles}

            <div class="zone-entry ${editing ? 'editing' : ''}">
                <div class="header">
                    <span class="zone-name"></span>
                    <span class="edit-btn-container"></span>
                </div>
                <div class="edit-zone ${!editing ? 'hidden' : ''}">
                    <div class="properties">
                        <label for="zone-name-input">Name</label>
                        <input id="zone-name-input" type="text">
                    </div>
                </div>
            </div>
            `;

        this.shadowRoot.querySelector('.zone-name').textContent = name;
        let input = this.shadowRoot.querySelector('input');
        if (input) input.value = name;

        let edit_button = document.createElement('button');
        edit_button.classList.add('edit-btn');
        edit_button.innerText = !editing ? 'Edit' : 'Save';
        edit_button.onclick = this.edit_event_handler.bind(this);

        if (!editing) {
            this.shadowRoot.querySelector('.edit-btn-container').appendChild(edit_button);
        } else {
            this.shadowRoot.querySelector('.edit-zone').appendChild(edit_button);
        }
    }


    edit_event_handler() {
        let editing = (this.getAttribute('editing') ?? 'false') === 'true';
        let name = this.shadowRoot.querySelector('input').value;

        this.dispatchEvent(new CustomEvent('edit', {
            bubbles: true,
            composed: true,
            detail: {editing: !editing, name: name, oldName: this.getAttribute('name')}
        }));
        this.setAttribute('editing', (!editing).toString());
    }


}

customElements.define('zone-entry', ZoneEntry);


/* Shared presentation; challenge generation and validation belong to the caller. */
function renderSecurityCaptcha(display, code) {
    display.replaceChildren();
    display.style.transform = `rotate(${Math.random() * 3 - 1.5}deg)`;
    display.style.letterSpacing = `${4 + Math.random() * 3}px`;
    for (const character of String(code)) {
        const digit = document.createElement('span');
        digit.textContent = character;
        digit.style.color = Math.random() > 0.5 ? '#0043ce' : '#0f62fe';
        digit.style.display = 'inline-block';
        digit.style.transform = `rotate(${Math.random() * 12 - 6}deg)`;
        display.appendChild(digit);
    }
}
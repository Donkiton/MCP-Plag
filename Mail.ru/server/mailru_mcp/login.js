const email=document.getElementById('email');
const password=document.getElementById('app-password');
const reveal=document.getElementById('reveal');
try{const saved=localStorage.getItem('mailru-login-address');if(saved)email.value=saved;}catch{}
reveal.addEventListener('click',()=>{const show=password.type==='password';password.type=show?'text':'password';reveal.setAttribute('aria-label',show?'Скрыть пароль':'Показать пароль');reveal.setAttribute('aria-pressed',String(show));});
document.getElementById('login-form').addEventListener('submit',()=>{try{localStorage.setItem('mailru-login-address',email.value.trim());}catch{}document.querySelector('.connect').disabled=true;});
document.getElementById('minimize').addEventListener('click',()=>{document.querySelector('.mail-window').classList.toggle('minimized');});
document.getElementById('maximize').addEventListener('click',()=>{document.querySelector('.mail-window').classList.toggle('maximized');});

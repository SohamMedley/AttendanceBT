// Small, event-driven animations. Polls never replay unchanged UI feedback.
window.provexMotion = {
  play(element, frames, duration = 420) {
    if (!element || !element.animate || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    element.getAnimations().forEach(animation => animation.cancel());
    element.animate(frames, {duration, easing:'cubic-bezier(.2,.8,.2,1)'});
  },
  enter(element) { this.play(element, [{opacity:0,transform:'translateY(9px)'},{opacity:1,transform:'translateY(0)'}]); },
  pulse(element) { this.play(element, [{transform:'scale(1)'},{transform:'scale(1.12)',color:'#34765a',offset:.4},{transform:'scale(1)'}], 500); },
  count(id, value) {
    const element = document.getElementById(id);
    if (element.textContent !== String(value)) {element.textContent=value;this.pulse(element);}
  },
  present(element) {this.play(element,[{backgroundColor:'#d7edca',transform:'translateY(4px)'},{backgroundColor:'#eff7e9',offset:.45},{backgroundColor:'transparent',transform:'translateY(0)'}],1400);}
};

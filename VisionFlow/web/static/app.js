/* VisionFlow - 视频复刻工具 前端交互 */

(function() {
  'use strict';

  document.addEventListener('DOMContentLoaded', function() {
    highlightNav();
  });

  function highlightNav() {
    var path = window.location.pathname;
    document.querySelectorAll('nav a').forEach(function(a) {
      var href = a.getAttribute('href');
      if (href === path || (path.startsWith('/project/') && href === '/')) {
        a.classList.add('bg-brand-600/20', 'text-brand-300');
        a.classList.remove('text-gray-400');
      }
    });
  }
})();

'use strict';
module.exports = function fitWindow(saved, area) {
  const minWidth = Math.min(860, area.width), minHeight = Math.min(560, area.height);
  const width = Math.min(area.width, Math.max(minWidth, Number.isFinite(saved.width) ? saved.width : 1420));
  const height = Math.min(area.height, Math.max(minHeight, Number.isFinite(saved.height) ? saved.height : 940));
  const x = Math.min(area.x + area.width - width, Math.max(area.x, Number.isFinite(saved.x) ? saved.x : area.x + (area.width - width) / 2));
  const y = Math.min(area.y + area.height - height, Math.max(area.y, Number.isFinite(saved.y) ? saved.y : area.y + (area.height - height) / 2));
  return { x: Math.round(x), y: Math.round(y), width: Math.round(width), height: Math.round(height), minWidth, minHeight };
};

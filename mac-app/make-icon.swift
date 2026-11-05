// Draws P2 Radar's 1024x1024 app icon: a dark squircle with "P2" set in heavy
// type, the 2 in Radar's blue, over a faint radar sweep. Usage: swift make-icon.swift out.png

import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"

func rgb(_ hex: Int, _ a: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 255) / 255, green: CGFloat((hex >> 8) & 255) / 255, blue: CGFloat(hex & 255) / 255, alpha: a)
}

let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
                           samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
let ctx = NSGraphicsContext.current!.cgContext

// Squircle body on Apple's icon grid (824pt artwork inside 1024).
let body = NSRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
ctx.saveGState()
ctx.setShadow(offset: CGSize(width: 0, height: -14), blur: 34, color: rgb(0x000000, 0.45).cgColor)
rgb(0x0d0d10).setFill()
shape.fill()
ctx.restoreGState()
shape.addClip()
NSGradient(colors: [rgb(0x26262d), rgb(0x0b0b0e)])!.draw(in: body, angle: -90)

let c = CGPoint(x: body.midX, y: body.midY)

// A faint radar sweep behind the letters: leading edge at 52°, 80° wide.
ctx.saveGState()
let wedge = NSBezierPath()
wedge.move(to: c)
wedge.appendArc(withCenter: c, radius: 380, startAngle: -28, endAngle: 52)
wedge.close()
wedge.addClip()
NSGradient(colors: [rgb(0x6b92ff, 0.0), rgb(0x6b92ff, 0.20)])!.draw(fromCenter: c, radius: 0, toCenter: c, radius: 380, options: [])
ctx.restoreGState()

// "P2" in heavy SF Pro, centred on its capitals; the 2 in Radar's blue.
let font = NSFont.systemFont(ofSize: 430, weight: .heavy)
let word = NSMutableAttributedString(string: "P", attributes: [.font: font, .foregroundColor: rgb(0xf2f2f2), .kern: -14])
word.append(NSAttributedString(string: "2", attributes: [.font: font, .foregroundColor: rgb(0x7ea0ff)]))
let width = word.size().width
word.draw(at: NSPoint(x: c.x - width / 2, y: c.y - font.capHeight / 2 + font.descender))

// Top-edge highlight.
let hl = NSBezierPath(roundedRect: body.insetBy(dx: 1.5, dy: 1.5), xRadius: 184, yRadius: 184)
hl.lineWidth = 3
NSGradient(colors: [rgb(0xffffff, 0.18), rgb(0xffffff, 0.0)])!.draw(in: hl, angle: -90)

NSGraphicsContext.restoreGraphicsState()
try! rep.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: out))
print("wrote \(out)")

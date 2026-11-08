using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;
using System.Windows.Forms;

public static class TrayDesign {
    // Shared palette for every window. Text tokens keep at least 4.5:1 contrast on
    // white cards and on PageColor; indicator tokens are for dots and icons only.
    public static readonly Color Blue=Color.FromArgb(32,108,198);          // #206CC6, white text 5.2:1
    public static readonly Color BlueSoft=Color.FromArgb(238,244,252);     // selected chips, link hover
    public static readonly Color BrandBlue=Color.FromArgb(40,116,236);     // app icon middle tone, not for text
    public static readonly Color Heading=Color.FromArgb(25,43,69);         // #192B45
    public static readonly Color TextColor=Color.FromArgb(44,62,80);       // #2C3E50
    public static readonly Color MutedColor=Color.FromArgb(95,110,130);    // #5F6E82
    public static readonly Color DisabledText=Color.FromArgb(138,150,163); // #8A96A3
    public static readonly Color PageColor=Color.FromArgb(243,245,249);    // #F3F5F9
    public static readonly Color CardColor=Color.White;
    public static readonly Color BorderColor=Color.FromArgb(229,234,238);
    public static readonly Color DividerColor=Color.FromArgb(232,236,239);
    public static readonly Color Success=Color.FromArgb(82,196,26);        // indicator
    public static readonly Color SuccessText=Color.FromArgb(47,125,70);    // #2F7D46
    public static readonly Color Warning=Color.FromArgb(201,134,8);        // indicator
    public static readonly Color WarningText=Color.FromArgb(153,96,0);     // #996000
    public static readonly Color Danger=Color.FromArgb(214,54,73);         // indicator
    public static readonly Color DangerText=Color.FromArgb(180,35,24);     // #B42318
    public static readonly Color Standby=Color.FromArgb(113,134,155);      // indicator
    public static readonly Color StandbyText=Color.FromArgb(82,106,128);   // #526A80
    static double Channel(int value) { double c=value/255.0; return c<=0.03928?c/12.92:Math.Pow((c+0.055)/1.055,2.4); }
    static double Luminance(Color c) { return 0.2126*Channel(c.R)+0.7152*Channel(c.G)+0.0722*Channel(c.B); }
    public static double Contrast(Color a, Color b) {
        double x=Luminance(a),y=Luminance(b);
        return (Math.Max(x,y)+0.05)/(Math.Min(x,y)+0.05);
    }
    // Darkens a bright indicator color toward TextColor until it is readable as text.
    public static Color Readable(Color color, Color background) {
        var result=Color.FromArgb(color.R,color.G,color.B);
        for(int step=1;step<=10 && Contrast(result,background)<4.5;step++)result=Mix(color,TextColor,step/10f);
        return result;
    }
    [DllImport("user32.dll")] static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    public static void EnableDpi() { try { SetProcessDpiAwarenessContext(new IntPtr(-4)); } catch {} }
    public static GraphicsPath Round(RectangleF r, float radius) {
        var p = new GraphicsPath(); float d = Math.Min(radius * 2,Math.Min(r.Width,r.Height));
        if(d<=0)return p;
        p.AddArc(r.X,r.Y,d,d,180,90); p.AddArc(r.Right-d,r.Y,d,d,270,90);
        p.AddArc(r.Right-d,r.Bottom-d,d,d,0,90); p.AddArc(r.X,r.Bottom-d,d,d,90,90); p.CloseFigure(); return p;
    }
    // Same shape language as tools/build_icon.py (64-unit coordinates). Up to 64 px it
    // uses the simplified tray geometry: one large bubble, no back bubble.
    public static Bitmap Symbol(Color color, int size, string state) {
        var b = new Bitmap(size,size);
        bool simple=size<=64;
        using(var g = Graphics.FromImage(b)) {
            g.SmoothingMode=SmoothingMode.AntiAlias; g.ScaleTransform(size/64f,size/64f);
            var square=simple?new RectangleF(2,2,60,60):new RectangleF(4,4,56,56);
            using(var bg=new LinearGradientBrush(square,Mix(color,Color.White,.24f),Mix(color,Color.Black,.10f),60f))
            using(var p=Round(square,simple?16:14.5f)) g.FillPath(bg,p);
            if(!simple) {
                using(var rear=new SolidBrush(Color.FromArgb(118,255,255,255))) {
                    using(var p=Round(new RectangleF(29,19.5f,24.5f,20),7)) g.FillPath(rear,p);
                    g.FillPolygon(rear,new[]{new PointF(44,37.5f),new PointF(51.5f,45.5f),new PointF(50,35)});
                }
            }
            var bubble=simple?new RectangleF(7.5f,15.5f,44.5f,30.5f):new RectangleF(10.5f,22.5f,35.5f,24);
            using(var p=Round(bubble,simple?10.5f:8.5f)) g.FillPath(Brushes.White,p);
            g.FillPolygon(Brushes.White,simple
                ?new[]{new PointF(13,42.5f),new PointF(11,54.5f),new PointF(25,44.5f)}
                :new[]{new PointF(15.5f,43.5f),new PointF(13.5f,53),new PointF(26,45)});
            Color ink=Readable(color,Color.White);
            float cx=bubble.X+bubble.Width/2,cy=bubble.Y+bubble.Height/2;
            using(var pen=new Pen(ink,simple?3.4f:2.6f)) {
                pen.StartCap=pen.EndCap=LineCap.Round;
                float r=simple?6.5f:5f;
                if(state=="error" || state=="error-stale") {g.DrawLine(pen,cx-r,cy-r,cx+r,cy+r);g.DrawLine(pen,cx+r,cy-r,cx-r,cy+r);}
                else if(state=="reconnecting" || state=="stopping" || state=="draining") {g.DrawLine(pen,cx,cy-r-1,cx,cy+r*.35f);g.DrawLine(pen,cx,cy+r+.5f,cx,cy+r+.6f);}
                else if(state=="stopped") {g.DrawLine(pen,cx-r-1,cy,cx+r+1,cy);}
                else using(var brush=new SolidBrush(ink)) {
                    g.FillPolygon(brush,Sparkle(simple?cx:cx-2,simple?cy+.25f:cy+.75f,simple?11f:9.5f));
                    if(!simple)g.FillPolygon(brush,Sparkle(cx+8,cy-6,3.8f));
                }
            }
        }
        return b;
    }
    // Four-point star with concave sides, matching the app icon's sparkle.
    public static PointF[] Sparkle(float cx, float cy, float radius) {
        var points=new PointF[72];
        for(int i=0;i<points.Length;i++) {
            double t=2*Math.PI*i/points.Length,c=Math.Cos(t),s=Math.Sin(t);
            points[i]=new PointF(cx+(float)(radius*Math.Sign(c)*Math.Pow(Math.Abs(c),2/.72)),cy+(float)(radius*Math.Sign(s)*Math.Pow(Math.Abs(s),2/.72)));
        }
        return points;
    }
    public static Bitmap Glyph(string kind, Color color, int size) {
        var b=new Bitmap(size,size);
        using(var g=Graphics.FromImage(b)) {g.SmoothingMode=SmoothingMode.AntiAlias;g.ScaleTransform(size/24f,size/24f);DrawGlyph(g,kind,color);}
        return b;
    }
    public static void DrawGlyph(Graphics g, string kind, Color color) {
        using(var pen=new Pen(color,1.6f)) {
            pen.StartCap=pen.EndCap=LineCap.Round;pen.LineJoin=LineJoin.Round;
            if(kind=="apps") {
                foreach(var r in new[]{new RectangleF(3,3,7,7),new RectangleF(14,3,7,7),new RectangleF(3,14,7,7),new RectangleF(14,14,7,7)})
                    using(var p=Round(r,1.5f)) g.DrawPath(pen,p);
            } else if(kind=="clock") {
                g.DrawEllipse(pen,3,3,18,18);g.DrawLines(pen,new[]{new PointF(12,7),new PointF(12,12),new PointF(16,12)});
            } else if(kind=="chat") {
                using(var p=Round(new RectangleF(3,4,18,14),4))g.DrawPath(pen,p);
                g.DrawLines(pen,new[]{new PointF(7,18),new PointF(5,21),new PointF(12,18)});
                g.DrawLine(pen,8,9,16,9);g.DrawLine(pen,8,13,13,13);
            } else if(kind=="send") {
                g.DrawPolygon(pen,new[]{new PointF(3,10),new PointF(21,3),new PointF(14,21),new PointF(10,14)});
                g.DrawLine(pen,10,14,21,3);
            } else if(kind=="cube") {
                g.DrawPolygon(pen,new[]{new PointF(12,2),new PointF(21,7),new PointF(21,17),new PointF(12,22),new PointF(3,17),new PointF(3,7)});
                g.DrawLines(pen,new[]{new PointF(3,7),new PointF(12,12),new PointF(21,7)});g.DrawLine(pen,12,12,12,22);
            } else if(kind=="activity") {
                g.DrawLines(pen,new[]{new PointF(2,12),new PointF(6,12),new PointF(9,4),new PointF(14,20),new PointF(17,12),new PointF(22,12)});
            } else if(kind=="shield") {
                g.DrawPolygon(pen,new[]{new PointF(12,2),new PointF(21,6),new PointF(20,15),new PointF(17,19),new PointF(12,22),new PointF(7,19),new PointF(4,15),new PointF(3,6)});
                g.DrawLines(pen,new[]{new PointF(8,11),new PointF(11,14),new PointF(16,9)});
            } else if(kind=="chevron") {
                g.DrawLines(pen,new[]{new PointF(9,5),new PointF(16,12),new PointF(9,19)});
            } else if(kind=="check") {
                g.DrawLines(pen,new[]{new PointF(5,12),new PointF(10,17),new PointF(19,7)});
            } else if(kind=="empty-task") {
                g.DrawLines(pen,new[]{new PointF(15,3),new PointF(5,3),new PointF(5,21),new PointF(19,21),new PointF(19,7),new PointF(15,3),new PointF(15,8),new PointF(19,8)});
                g.DrawLine(pen,8,12,15,12);g.DrawLine(pen,8,16,12,16);
            } else if(kind=="restart") {
                g.DrawArc(pen,3,3,18,18,35,285);g.DrawLines(pen,new[]{new PointF(16,3),new PointF(21,3),new PointF(21,8)});
            } else if(kind=="settings") {
                g.DrawEllipse(pen,4,4,16,16);g.DrawEllipse(pen,9,9,6,6);
                for(int i=0;i<8;i++){double a=i*Math.PI/4;g.DrawLine(pen,12+(float)Math.Cos(a)*8,12+(float)Math.Sin(a)*8,12+(float)Math.Cos(a)*10.5f,12+(float)Math.Sin(a)*10.5f);}
            } else if(kind=="history") {
                g.DrawArc(pen,4,3,17,18,205,305);g.DrawLines(pen,new[]{new PointF(3,4),new PointF(3,9),new PointF(8,9)});
                g.DrawLines(pen,new[]{new PointF(12,7),new PointF(12,12),new PointF(16,14)});
            } else if(kind=="power") {
                g.DrawArc(pen,4,4,16,17,-45,270);g.DrawLine(pen,12,2,12,11);
            } else if(kind=="stop") {
                using(var p=Round(new RectangleF(6,6,12,12),1.5f))g.DrawPath(pen,p);
            } else if(kind=="log") {
                using(var p=Round(new RectangleF(4,3,16,18),2))g.DrawPath(pen,p);
                for(int y=8;y<=16;y+=4){g.DrawLine(pen,8,y,8.2f,y);g.DrawLine(pen,11,y,16,y);}
            } else if(kind=="folder") {
                g.DrawLines(pen,new[]{new PointF(3,18),new PointF(3,6),new PointF(9,6),new PointF(11,8.5f),new PointF(21,8.5f),new PointF(21,18),new PointF(3,18)});
                g.DrawLine(pen,3,11.5f,21,11.5f);
            } else if(kind=="bookmark") {
                g.DrawLines(pen,new[]{new PointF(6,3),new PointF(18,3),new PointF(18,21),new PointF(12,16.5f),new PointF(6,21),new PointF(6,3)});
            } else if(kind=="clip") {
                g.DrawLines(pen,new[]{new PointF(16.5f,7),new PointF(9,14.5f),new PointF(9,16),new PointF(11,18),new PointF(12.5f,18),new PointF(20,10.5f),new PointF(20,7),new PointF(17,4),new PointF(14,4),new PointF(5.5f,12.5f),new PointF(5.5f,17),new PointF(9,20.5f),new PointF(13,20.5f),new PointF(15,18.5f)});
            } else if(kind=="task") {
                g.DrawLines(pen,new[]{new PointF(8,4),new PointF(5,4),new PointF(5,21),new PointF(19,21),new PointF(19,4),new PointF(16,4)});
                using(var p=Round(new RectangleF(8,2,8,4),1))g.DrawPath(pen,p);
                g.DrawLine(pen,9,11,15,11);g.DrawLine(pen,9,15,15,15);
            } else {
                using(var p=Round(new RectangleF(5,3,14,18),2))g.DrawPath(pen,p);
                g.DrawLine(pen,9,8,15,8);g.DrawLine(pen,9,12,15,12);g.DrawLine(pen,9,16,13,16);
            }
        }
    }
    public static void StyleFields(Control root) {
        foreach(Control control in root.Controls) {
            if(control is TextBox || control is ComboBox || control is ListView) {
                control.Font=new Font("Microsoft YaHei UI",9.5f);control.ForeColor=TextColor;control.BackColor=Color.White;
                var text=control as TextBox;if(text!=null)text.BorderStyle=BorderStyle.FixedSingle;
                var list=control as ListView;if(list!=null){list.BorderStyle=BorderStyle.FixedSingle;list.GridLines=false;}
            }
            if(control.HasChildren)StyleFields(control);
        }
    }
    public static void SaveShortcut(Form window, Button save) {
        window.KeyPreview=true;
        window.KeyDown+=(sender,e)=>{if(e.Control && e.KeyCode==Keys.S){e.SuppressKeyPress=true;e.Handled=true;if(save.Enabled)save.PerformClick();}};
    }
    public static Color SurfaceFor(Control control) {
        for(var current=control;current!=null;current=current.Parent) {
            var card=current as TrayCard;
            if(card!=null)return card.Surface;
            if(current.BackColor.A==255)return current.BackColor;
        }
        return Color.White;
    }
    // Opt in per window so the shared settings dialogs keep their existing styling.
    public static void EnableDepth(Control root) {
        var card=root as TrayCard;if(card!=null)card.Depth=true;
        var button=root as TrayButton;if(button!=null)button.Depth=true;
        foreach(Control child in root.Controls)EnableDepth(child);
    }
    public static Color Mix(Color color, Color target, float amount) {
        return Color.FromArgb((int)(color.R+(target.R-color.R)*amount),(int)(color.G+(target.G-color.G)*amount),(int)(color.B+(target.B-color.B)*amount));
    }
    public static void SoftShadow(Graphics graphics, RectangleF face, float radius, float scale, Color tint, int opacity) {
        // Three faint layers stay inside the control bounds; no extra layout space is needed.
        for(int layer=3;layer>=1;layer--) {
            var shadow=face;shadow.Offset(0,layer*scale);
            using(var path=Round(shadow,radius))
            using(var brush=new SolidBrush(Color.FromArgb(opacity,tint)))graphics.FillPath(brush,path);
        }
    }
}
// Tray context menu: white surface, rounded hover, inset separators, palette colors.
public class TrayMenuColors : ProfessionalColorTable {
    public override Color ToolStripDropDownBackground {get{return TrayDesign.CardColor;}}
    public override Color ImageMarginGradientBegin {get{return TrayDesign.CardColor;}}
    public override Color ImageMarginGradientMiddle {get{return TrayDesign.CardColor;}}
    public override Color ImageMarginGradientEnd {get{return TrayDesign.CardColor;}}
    public override Color MenuBorder {get{return Color.FromArgb(214,221,229);}}
    public override Color MenuItemBorder {get{return TrayDesign.BlueSoft;}}
    public override Color MenuItemSelected {get{return TrayDesign.BlueSoft;}}
    public override Color SeparatorDark {get{return TrayDesign.DividerColor;}}
    public override Color SeparatorLight {get{return TrayDesign.CardColor;}}
}
public class TrayMenuRenderer : ToolStripProfessionalRenderer {
    public TrayMenuRenderer() : base(new TrayMenuColors()) { RoundedEdges=false; }
    static float Scale(Graphics g){return g.DpiX/96f;}
    protected override void OnRenderToolStripBackground(ToolStripRenderEventArgs e) {
        using(var brush=new SolidBrush(TrayDesign.CardColor))e.Graphics.FillRectangle(brush,e.AffectedBounds);
    }
    protected override void OnRenderImageMargin(ToolStripRenderEventArgs e) {
        using(var brush=new SolidBrush(TrayDesign.CardColor))e.Graphics.FillRectangle(brush,e.AffectedBounds);
    }
    protected override void OnRenderMenuItemBackground(ToolStripItemRenderEventArgs e) {
        if(!e.Item.Selected && !e.Item.Pressed)return;
        var g=e.Graphics;g.SmoothingMode=SmoothingMode.AntiAlias;float s=Scale(g);
        var face=new RectangleF(4*s,1*s,e.Item.Width-8*s,e.Item.Height-2*s);
        using(var path=TrayDesign.Round(face,6*s))
        using(var brush=new SolidBrush(e.Item.Enabled?TrayDesign.BlueSoft:Color.FromArgb(246,247,249)))g.FillPath(brush,path);
    }
    protected override void OnRenderItemText(ToolStripItemTextRenderEventArgs e) {
        e.TextColor=!e.Item.Enabled?TrayDesign.DisabledText:(e.Item is ToolStripLabel?TrayDesign.Heading:TrayDesign.TextColor);
        base.OnRenderItemText(e);
    }
    protected override void OnRenderSeparator(ToolStripSeparatorRenderEventArgs e) {
        float s=Scale(e.Graphics);int y=e.Item.Height/2;
        using(var pen=new Pen(TrayDesign.DividerColor))e.Graphics.DrawLine(pen,(int)(12*s),y,e.Item.Width-(int)(12*s),y);
    }
    protected override void OnRenderArrow(ToolStripArrowRenderEventArgs e) {
        e.ArrowColor=e.Item==null || e.Item.Enabled?TrayDesign.MutedColor:TrayDesign.DisabledText;
        base.OnRenderArrow(e);
    }
    protected override void OnRenderItemCheck(ToolStripItemImageRenderEventArgs e) {
        var g=e.Graphics;g.SmoothingMode=SmoothingMode.AntiAlias;var r=e.ImageRectangle;
        using(var path=TrayDesign.Round(new RectangleF(r.X-1,r.Y-1,r.Width+2,r.Height+2),4*Scale(g)))
        using(var brush=new SolidBrush(TrayDesign.BlueSoft))g.FillPath(brush,path);
        var saved=g.Save();g.TranslateTransform(r.X,r.Y);g.ScaleTransform(r.Width/24f,r.Height/24f);
        TrayDesign.DrawGlyph(g,"check",e.Item.Enabled?TrayDesign.Blue:TrayDesign.DisabledText);g.Restore(saved);
    }
}
public static class TrayMenuStyle {
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd,int attribute,ref int value,int size);
    // Windows 11 rounds popup corners on request; older systems ignore the call.
    static void RoundCorners(object sender,EventArgs e) {
        try { int round=2; DwmSetWindowAttribute(((Control)sender).Handle,33,ref round,4); } catch {}
    }
    public static void Apply(ToolStripDropDown menu, float scale) {
        menu.Renderer=new TrayMenuRenderer();
        menu.ImageScalingSize=new Size((int)Math.Round(18*scale),(int)Math.Round(18*scale));
        menu.Padding=new Padding(0,(int)(4*scale),0,(int)(4*scale));
        menu.HandleCreated+=RoundCorners;
        foreach(ToolStripItem item in menu.Items) {
            if(item is ToolStripSeparator)continue;
            item.Padding=new Padding(0,(int)(3*scale),0,(int)(3*scale));
            var sub=item as ToolStripMenuItem;
            if(sub!=null && sub.HasDropDownItems)Apply(sub.DropDown,scale);
        }
    }
}
public class TrayBadge : Control {
    public Color StatusColor {get;set;}
    public bool Navigate {get;set;}
    public TrayBadge(){DoubleBuffered=true;SetStyle(ControlStyles.ResizeRedraw,true);StatusColor=TrayDesign.Blue;Font=new Font("Microsoft YaHei UI",9);}
    protected override void OnPaint(PaintEventArgs e){
        Color surface=TrayDesign.SurfaceFor(Parent),textColor=TrayDesign.Readable(StatusColor,surface);
        e.Graphics.Clear(surface);e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;float scale=e.Graphics.DpiX/96f;
        using(var p=TrayDesign.Round(new RectangleF(.5f,.5f,Width-1,Height-1),(Height-1)/2f)) {
            using(var brush=new SolidBrush(TrayDesign.Mix(surface,StatusColor,.08f)))e.Graphics.FillPath(brush,p);
            using(var pen=new Pen(TrayDesign.Mix(surface,StatusColor,.22f)))e.Graphics.DrawPath(pen,p);
        }
        using(var brush=new SolidBrush(StatusColor))e.Graphics.FillEllipse(brush,12*scale,(Height-8*scale)/2,8*scale,8*scale);
        TextRenderer.DrawText(e.Graphics,Text,Font,new Rectangle((int)(27*scale),0,Math.Max(0,Width-(int)((Navigate?52:34)*scale)),Height),textColor,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
        if(Navigate){var saved=e.Graphics.Save();e.Graphics.TranslateTransform(Width-28*scale,(Height-16*scale)/2);e.Graphics.ScaleTransform(16*scale/24,16*scale/24);TrayDesign.DrawGlyph(e.Graphics,"chevron",textColor);e.Graphics.Restore(saved);}
    }
}
public class TrayCard : Panel {
    public Color Surface {get;set;}
    public Color Border {get;set;}
    public bool Depth {get;set;}
    public TrayCard() { DoubleBuffered=true;SetStyle(ControlStyles.ResizeRedraw,true); BackColor=Color.Transparent; Surface=TrayDesign.CardColor; Border=TrayDesign.BorderColor; }
    protected override void OnPaintBackground(PaintEventArgs e) {
        e.Graphics.Clear(TrayDesign.SurfaceFor(Parent)); e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
        float scale=e.Graphics.DpiX/96f;
        var face=new RectangleF(.5f,.5f,Width-1,Math.Max(1,Height-1-(Depth?3*scale:0)));
        if(Depth)TrayDesign.SoftShadow(e.Graphics,face,12*scale,scale,Color.FromArgb(46,68,94),8);
        using(var p=TrayDesign.Round(face,12*scale)) {
            using(var brush=new SolidBrush(Surface)) e.Graphics.FillPath(brush,p);
            using(var pen=new Pen(Border)) e.Graphics.DrawPath(pen,p);
        }
        if(Depth) {
            face.Inflate(-scale,-scale);
            using(var p=TrayDesign.Round(face,11*scale))
            using(var pen=new Pen(Color.FromArgb(210,255,255,255),scale))e.Graphics.DrawPath(pen,p);
        }
    }
}
public class TrayButton : Button {
    public bool Primary {get;set;}
    public bool Quiet {get;set;}
    public bool Danger {get;set;}
    public bool Navigate {get;set;}
    public bool ToggleChip {get;set;}
    // Borderless text action for card headers; keeps the card from filling up with outlines.
    public bool Link {get;set;}
    public bool Depth {get;set;}
    public string Description {get;set;}
    public string IconKind {get;set;}
    bool hover;
    bool pressed;
    public TrayButton() { SetStyle(ControlStyles.UserPaint|ControlStyles.AllPaintingInWmPaint|ControlStyles.OptimizedDoubleBuffer|ControlStyles.ResizeRedraw,true); Cursor=Cursors.Hand; ForeColor=TrayDesign.TextColor; }
    protected override void OnMouseEnter(EventArgs e){hover=true;Invalidate();base.OnMouseEnter(e);}
    protected override void OnMouseLeave(EventArgs e){hover=false;Invalidate();base.OnMouseLeave(e);}
    protected override void OnMouseDown(MouseEventArgs e){if(e.Button==MouseButtons.Left)pressed=true;Invalidate();base.OnMouseDown(e);}
    protected override void OnMouseUp(MouseEventArgs e){pressed=false;Invalidate();base.OnMouseUp(e);}
    protected override void OnMouseCaptureChanged(EventArgs e){if(!Capture)pressed=false;Invalidate();base.OnMouseCaptureChanged(e);}
    protected override void OnKeyDown(KeyEventArgs e){if(e.KeyCode==Keys.Space)pressed=true;Invalidate();base.OnKeyDown(e);}
    protected override void OnKeyUp(KeyEventArgs e){if(e.KeyCode==Keys.Space)pressed=false;Invalidate();base.OnKeyUp(e);}
    protected override void OnGotFocus(EventArgs e){Invalidate();base.OnGotFocus(e);}
    protected override void OnLostFocus(EventArgs e){pressed=false;Invalidate();base.OnLostFocus(e);}
    protected override void OnChangeUICues(UICuesEventArgs e){Invalidate();base.OnChangeUICues(e);}
    protected override void OnEnabledChanged(EventArgs e){pressed=false;Invalidate();base.OnEnabledChanged(e);}
    protected override void OnPaint(PaintEventArgs e){
        Color surface=TrayDesign.SurfaceFor(Parent);
        e.Graphics.Clear(surface);e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
        float scale=e.Graphics.DpiX/96f;
        bool strong=Primary && !Quiet && !ToggleChip, selected=Primary && (Quiet || ToggleChip);
        bool link=Link && !strong && !selected && !Danger;
        Color fill=Color.White,border=TrayDesign.BorderColor,textColor=ForeColor;
        if(strong){
            fill=pressed?TrayDesign.Mix(TrayDesign.Blue,Color.Black,.22f):(hover?TrayDesign.Mix(TrayDesign.Blue,Color.Black,.12f):TrayDesign.Blue);
            border=fill;textColor=Color.White;
        }else if(selected){
            fill=pressed?TrayDesign.Mix(TrayDesign.BlueSoft,TrayDesign.Blue,.10f):(hover?TrayDesign.Mix(TrayDesign.BlueSoft,TrayDesign.Blue,.05f):TrayDesign.BlueSoft);
            border=TrayDesign.Mix(TrayDesign.BlueSoft,TrayDesign.Blue,.28f);textColor=TrayDesign.Blue;
        }else if(link){
            fill=pressed?TrayDesign.Mix(TrayDesign.BlueSoft,TrayDesign.Blue,.08f):(hover?TrayDesign.BlueSoft:surface);
            border=fill;textColor=TrayDesign.Blue;
        }else{
            fill=pressed?Color.FromArgb(236,240,244):(hover?Color.FromArgb(247,249,251):Color.White);
            if(hover)border=Color.FromArgb(199,210,219);
        }
        if(Danger && !strong){
            fill=pressed?Color.FromArgb(251,235,233):(hover?Color.FromArgb(255,244,242):Color.FromArgb(255,249,248));
            border=Color.FromArgb(236,212,209);textColor=TrayDesign.DangerText;
        }
        if(!Enabled){
            fill=link?surface:Color.FromArgb(243,245,247);
            border=link?surface:Color.FromArgb(233,237,240);textColor=TrayDesign.DisabledText;
        }
        bool raised=Depth && Enabled && !ToggleChip && !link;
        float shadowSpace=raised?3*scale:0;
        var face=new RectangleF(.5f,.5f+(raised && pressed?scale:0),Width-1,Math.Max(1,Height-1-shadowSpace));
        if(raised && !pressed)TrayDesign.SoftShadow(e.Graphics,face,8*scale,scale,strong?TrayDesign.Blue:Color.FromArgb(46,68,94),strong?(hover?15:11):(hover?9:6));
        using(var p=TrayDesign.Round(face,8*scale)){
            if(raised && !pressed) {
                Color top=TrayDesign.Mix(fill,Color.White,strong ? .12f : 0f),bottom=TrayDesign.Mix(fill,strong?Color.FromArgb(30,90,164):Color.FromArgb(210,222,236),strong ? .06f : .09f);
                using(var b=new LinearGradientBrush(face,top,bottom,LinearGradientMode.Vertical))e.Graphics.FillPath(b,p);
            }else using(var b=new SolidBrush(fill))e.Graphics.FillPath(b,p);
            using(var pen=new Pen(border))e.Graphics.DrawPath(pen,p);
        }
        if(raised && !pressed) {
            var highlight=face;highlight.Inflate(-scale,-scale);
            using(var p=TrayDesign.Round(highlight,7*scale))
            using(var pen=new Pen(Color.FromArgb(strong?42:190,255,255,255),scale))e.Graphics.DrawPath(pen,p);
        }
        bool described=!String.IsNullOrEmpty(Description),icon=!String.IsNullOrEmpty(IconKind);
        int left=icon?(int)((described?48:40)*scale):(int)(12*scale);
        bool trailing=Navigate || ToggleChip;
        int right=(int)((trailing?36:12)*scale);
        if(icon){
            float glyphSize=20*scale,iconLeft=16*scale;
            Color glyphColor=Enabled && !strong && !Danger?TrayDesign.Blue:textColor;
            var saved=e.Graphics.Save();e.Graphics.TranslateTransform(iconLeft,(Height-glyphSize)/2);e.Graphics.ScaleTransform(glyphSize/24,glyphSize/24);TrayDesign.DrawGlyph(e.Graphics,IconKind,glyphColor);e.Graphics.Restore(saved);
        }
        if(described){
            using(var small=new Font(Font.FontFamily,Math.Max(8.5f,Font.Size-1f))){
                int titleHeight=(int)Math.Ceiling(Math.Max(18*scale,Font.GetHeight(e.Graphics)+scale));
                int detailHeight=(int)Math.Ceiling(Math.Max(14*scale,small.GetHeight(e.Graphics)+scale));
                int gap=(int)(2*scale),top=Math.Max(0,(Height-titleHeight-detailHeight-gap)/2),width=Math.Max(0,Width-left-right);
                Color detailColor=!Enabled?TrayDesign.DisabledText:(strong?Color.FromArgb(235,244,255):TrayDesign.MutedColor);
                TextRenderer.DrawText(e.Graphics,Text,Font,new Rectangle(left,top,width,titleHeight),textColor,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
                TextRenderer.DrawText(e.Graphics,Description,small,new Rectangle(left,top+titleHeight+gap,width,detailHeight),detailColor,TextFormatFlags.Left|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
            }
        }else TextRenderer.DrawText(e.Graphics,Text,Font,icon || trailing?new Rectangle(left,0,Math.Max(0,Width-left-right),Height):ClientRectangle,textColor,(icon || trailing?TextFormatFlags.Left:TextFormatFlags.HorizontalCenter)|TextFormatFlags.VerticalCenter|TextFormatFlags.EndEllipsis);
        if(Navigate || (ToggleChip && Primary)){
            var saved=e.Graphics.Save();float glyphSize=16*scale;e.Graphics.TranslateTransform(Width-28*scale,(Height-glyphSize)/2);e.Graphics.ScaleTransform(glyphSize/24,glyphSize/24);
            TrayDesign.DrawGlyph(e.Graphics,Navigate?"chevron":"check",Navigate && !strong?TrayDesign.MutedColor:textColor);e.Graphics.Restore(saved);
        }
        if(Focused && ShowFocusCues){
            float inset=3.5f*scale;
            using(var p=TrayDesign.Round(new RectangleF(inset,inset,Width-2*inset,Height-2*inset),5*scale))
            using(var pen=new Pen(strong && Enabled?Color.FromArgb(206,227,251):TrayDesign.Blue,1.5f*scale))e.Graphics.DrawPath(pen,p);
        }
    }
}
public class TrayTimelineMarks : Control {
    TextBox source;
    TimelineWindow scrollWindow;
    public TextBox Source {
        get{return source;}
        set {
            if(source==value)return;
            DetachSource();source=value;
            if(source!=null){
                source.TextChanged+=SourceChanged;source.SizeChanged+=SourceChanged;source.FontChanged+=SourceChanged;
                source.HandleCreated+=SourceHandleCreated;source.HandleDestroyed+=SourceHandleDestroyed;
                AttachWindow();
            }
            Invalidate();
        }
    }
    public TrayTimelineMarks(){DoubleBuffered=true;SetStyle(ControlStyles.ResizeRedraw|ControlStyles.SupportsTransparentBackColor,true);BackColor=Color.Transparent;TabStop=false;}
    void SourceChanged(object sender,EventArgs e){Invalidate();}
    void SourceHandleCreated(object sender,EventArgs e){AttachWindow();Invalidate();}
    void SourceHandleDestroyed(object sender,EventArgs e){ReleaseWindow();}
    void AttachWindow(){ReleaseWindow();if(source!=null && source.IsHandleCreated){scrollWindow=new TimelineWindow(this);scrollWindow.AssignHandle(source.Handle);}}
    void ReleaseWindow(){if(scrollWindow!=null){scrollWindow.ReleaseHandle();scrollWindow=null;}}
    void DetachSource(){
        ReleaseWindow();
        if(source!=null){
            source.TextChanged-=SourceChanged;source.SizeChanged-=SourceChanged;source.FontChanged-=SourceChanged;
            source.HandleCreated-=SourceHandleCreated;source.HandleDestroyed-=SourceHandleDestroyed;
        }
    }
    protected override void Dispose(bool disposing){if(disposing)DetachSource();base.Dispose(disposing);}
    protected override void OnPaintBackground(PaintEventArgs e){e.Graphics.Clear(TrayDesign.SurfaceFor(Parent));}
    protected override void OnPaint(PaintEventArgs e){
        if(source==null || source.IsDisposed || !source.IsHandleCreated || String.IsNullOrWhiteSpace(source.Text))return;
        e.Graphics.SmoothingMode=SmoothingMode.AntiAlias;
        float scale=e.Graphics.DpiX/96f,lineHeight=source.Font.GetHeight(e.Graphics),x=Width/2f;
        var centers=new System.Collections.Generic.List<float>();
        string content=source.Text;int start=0;
        while(start<content.Length){
            int end=content.IndexOf('\n',start);if(end<0)end=content.Length;
            if(!String.IsNullOrWhiteSpace(content.Substring(start,end-start))){
                int line=source.GetLineFromCharIndex(start),first=source.GetFirstCharIndexFromLine(line);
                if(first>=0)centers.Add(source.GetPositionFromCharIndex(first).Y+lineHeight/2);
            }
            start=end+1;
        }
        if(centers.Count==0)return;
        float top=Math.Max(0,centers[0]),bottom=Math.Min(Height,centers[centers.Count-1]);
        if(bottom>top)using(var pen=new Pen(Color.FromArgb(224,233,242),scale))e.Graphics.DrawLine(pen,x,top,x,bottom);
        float radius=2.5f*scale;
        using(var brush=new SolidBrush(TrayDesign.Blue))foreach(float y in centers)if(y>=-radius && y<=Height+radius)e.Graphics.FillEllipse(brush,x-radius,y-radius,radius*2,radius*2);
    }
    sealed class TimelineWindow : NativeWindow {
        readonly TrayTimelineMarks owner;
        public TimelineWindow(TrayTimelineMarks owner){this.owner=owner;}
        protected override void WndProc(ref Message m){
            base.WndProc(ref m);
            // Paint from the text box's actual viewport after scrolling or caret navigation.
            if(m.Msg==0x115 || m.Msg==0x20A || m.Msg==0x101 || m.Msg==0x113 || m.Msg==0x202 || m.Msg==0xB1 || m.Msg==0xB5 || m.Msg==0xB6 || m.Msg==0xB7)
                if(!owner.IsDisposed)owner.Invalidate();
        }
    }
}

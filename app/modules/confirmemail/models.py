from app import db


class Confirmemail(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    def __repr__(self):
        return f'Confirmemail<{self.id}>'

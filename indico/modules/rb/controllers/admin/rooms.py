# -*- coding: utf-8 -*-
##
##
## This file is part of Indico
## Copyright (C) 2002 - 2013 European Organization for Nuclear Research (CERN)
##
## Indico is free software: you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation, either version 3 of the
## License, or (at your option) any later version.
##
## Indico is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
## GNU General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Indico.  If not, see <http://www.gnu.org/licenses/>.

from flask import request, flash
from wtforms import TextField
from wtforms.validators import DataRequired
from MaKaC.common.cache import GenericCache
from MaKaC.user import AvatarHolder
from MaKaC.webinterface import urlHandlers as UH

from indico.core.db import db
from indico.core.errors import NotFoundError
from indico.modules.rb.controllers.admin import RHRoomBookingAdminBase
from indico.modules.rb.forms.base import FormDefaults
from indico.modules.rb.forms.rooms import RoomForm
from indico.modules.rb.forms.validators import IndicoEmail
from indico.modules.rb.models.room_equipments import RoomEquipment
from indico.modules.rb.models.room_attributes import RoomAttributeAssociation, RoomAttribute
from indico.modules.rb.controllers.decorators import requires_location, requires_room
from indico.modules.rb.models.room_bookable_times import BookableTime
from indico.modules.rb.models.room_nonbookable_dates import NonBookableDate
from indico.modules.rb.models.rooms import Room
from indico.modules.rb.models.photos import Photo
from indico.modules.rb.views.admin import rooms as room_views
from indico.util.i18n import _
from indico.web.flask.util import url_for

_cache = GenericCache('Rooms')


class RHRoomBookingDeleteRoom(RHRoomBookingAdminBase):
    def _checkParams(self):
        self._room = Room.get(request.view_args['roomID'])
        self._target = self._room

    def _process(self):
        if self._room.has_live_reservations():
            flash(_(u'Cannot delete room with live bookings'), 'error')
            self._redirect(UH.UHRoomBookingRoomDetails.getURL(self._room))
        else:
            db.session.delete(self._room)
            flash(_(u'Room deleted'), 'success')
            self._redirect(url_for('rooms_admin.roomBooking-adminLocation', self._room.location))


class RHRoomBookingCreateModifyRoomBase(RHRoomBookingAdminBase):
    def _make_form(self):
        room = self._room

        # New class so we can safely extend it
        form_class = type('RoomFormWithAttributes', (RoomForm,), {})

        # Default values
        defaults = None
        if room.id is not None:
            skip_name = set() if room.has_special_name else {'name'}
            defaults = FormDefaults(room, skip_attrs={'nonbookable_dates', 'bookable_times'} | skip_name)
            for ra in room.attributes.all():
                defaults['attribute_{}'.format(ra.attribute_id)] = ra.value

        # Custom attributes - new fields must be set on the class
        for attribute in self._location.attributes.order_by(RoomAttribute.parent_id).all():
            validators = [DataRequired()] if attribute.is_required else []
            if attribute.name == 'notification-email':
                validators.append(IndicoEmail(multi=True))
            field_name = 'attribute_{}'.format(attribute.id)
            field = TextField(attribute.title, validators)
            setattr(form_class, field_name, field)

        # Create the form
        form = form_class(obj=defaults)

        # Default values, part 2
        if not form.is_submitted() and room.id is not None:
            # This is ugly, but apparently FieldList+FormField does not work well with obj defaults
            for i, nbd in enumerate(room.nonbookable_dates.all()):
                if i >= len(form.nonbookable_dates.entries):
                    form.nonbookable_dates.append_entry()
                form.nonbookable_dates[i].start.data = nbd.start_dt
                form.nonbookable_dates[i].end.data = nbd.end_dt

            for i, bt in enumerate(room.bookable_times.all()):
                if i >= len(form.bookable_times.entries):
                    form.bookable_times.append_entry()
                form.bookable_times[i].start.data = bt.start_time
                form.bookable_times[i].end.data = bt.end_time

        # Custom attributes, part 2
        form._attribute_fields = [field for name, field in form._fields.iteritems() if name.startswith('attribute_')]

        # Equipment
        form.equipments.query = self._location.equipment_objects.order_by(RoomEquipment.name)

        return form

    def _save(self):
        room = self._room
        form = self._form
        # Simple fields
        form.populate_obj(room, skip=('bookable_times', 'nonbookable_dates'), existing_only=True)
        room.update_name()
        # Photos
        if form.small_photo.data and form.large_photo.data:
            _cache.delete_multi('photo-{}-{}'.format(room.id, size) for size in {'small', 'large'})
            room.photo = Photo(thumbnail=form.small_photo.data.read(), data=form.large_photo.data.read())
        elif form.delete_photos.data:
            _cache.delete_multi('photo-{}-{}'.format(room.id, size) for size in {'small', 'large'})
            room.photo = None
        # Custom attributes
        room.attributes = [RoomAttributeAssociation(value=form['attribute_{}'.format(attr.id)].data,
                                                    attribute_id=attr.id)
                           for attr in self._location.attributes.all()
                           if form['attribute_{}'.format(attr.id)].data]
        # Bookable times
        room.bookable_times = [BookableTime(start_time=bt['start'], end_time=bt['end'])
                               for bt in form.bookable_times.data if all(bt.viewvalues())]
        # Nonbookable dates
        room.nonbookable_dates = [NonBookableDate(start_dt=nbd['start'], end_dt=nbd['end'])
                                  for nbd in form.nonbookable_dates.data if all(nbd.viewvalues())]

    def _process(self):
        room_owner = None
        if self._form.owner_id.data:
            room_owner = AvatarHolder().getById(self._form.owner_id.data)
        elif self._room.id is not None:
            room_owner = self._room.getResponsible()

        if self._form.validate_on_submit():
            self._save()
            self._redirect(UH.UHRoomBookingRoomDetails.getURL(self._room))
        else:
            return room_views.WPRoomBookingRoomForm(self, form=self._form, room=self._room, location=self._location,
                                                    errors=self._form.error_list, room_owner=room_owner).display()


class RHRoomBookingModifyRoom(RHRoomBookingCreateModifyRoomBase):
    @requires_location(parameter_name='roomLocation')
    @requires_room
    def _checkParams(self):
        self._form = self._make_form()

    def _save(self):
        RHRoomBookingCreateModifyRoomBase._save(self)
        flash(_(u'Room updated'), 'success')


class RHRoomBookingCreateRoom(RHRoomBookingCreateModifyRoomBase):
    @requires_location(parameter_name='roomLocation')
    def _checkParams(self):
        self._room = Room()
        self._form = self._make_form()

    def _save(self):
        RHRoomBookingCreateModifyRoomBase._save(self)
        self._room.location = self._location
        db.session.add(self._room)
        db.session.flush()
        flash(_(u'Room added'), 'success')
